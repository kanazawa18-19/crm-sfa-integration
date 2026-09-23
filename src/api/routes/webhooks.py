"""Webhook受信エンドポイント群（2026-08-28にsrc/api/app.pyから分割）。

kintone/Zoho/Notion/Gmail(Pub/Sub)/Slack/MA(web-engagement)から叩かれる。**パスは各ツール側の
Webhook購読設定に登録済みの宛先**であり、変えると通知が届かなくなる。
`tests/api/test_route_registry.py`がパスの集合を固定している。

認証は各handler内部の共有シークレット検証で行う（多くはX-Webhook-Secretヘッダー、
`src/sync_engine/webhook_handlers/_common.py`のverify_webhook_secret）。ただしZoho
（カスタムヘッダー・bodyへの任意フィールド追加のいずれも不可、body内tokenフィールド方式、
verify_webhook_body_token）・kintone（カスタムヘッダー不可、URLクエリパラメータ方式、
verify_webhook_query_param）は、外部ツール側のWebhook機能の制約により別方式を使う。
"""

from __future__ import annotations

import asyncio
import json
import logging
import weakref
from typing import Any, Callable, Mapping, NamedTuple

import anyio
from fastapi import APIRouter, Depends, Request, Response

from src.api.dependencies import wiring_dependency
from src.sync_engine import webhook_receipts
from src.sync_engine.production_wiring import ProductionSyncWiring
from src.sync_engine.webhook_handlers.gmail_push_webhook import (
    handler as gmail_push_webhook_handler,
)
from src.sync_engine.webhook_handlers.kintone_webhook import handler as kintone_webhook_handler
from src.sync_engine.webhook_handlers.lead_inquiry_webhook import (
    handler as lead_inquiry_webhook_handler,
)
from src.sync_engine.webhook_handlers.notion_webhook import (
    handler_with_proxy as notion_webhook_handler_with_proxy,
)
from src.sync_engine.webhook_handlers.slack_interaction_webhook import (
    handler as slack_interaction_webhook_handler,
)
from src.sync_engine.webhook_handlers.spreadsheet_webhook import (
    handler as spreadsheet_webhook_handler,
)
from src.sync_engine.webhook_handlers.web_engagement_meeting_webhook import (
    handler as web_engagement_meeting_webhook_handler,
)
from src.sync_engine.webhook_handlers.web_engagement_webhook import (
    handler as web_engagement_webhook_handler,
)
from src.sync_engine.webhook_handlers.zoho_webhook import handler as zoho_webhook_handler

logger = logging.getLogger(__name__)

router = APIRouter()



async def _lambda_event_from_request(request: Request) -> dict[str, Any]:
    """FastAPIの`Request`を、Webhookハンドラ（Lambda形式）が期待する`event`辞書へ変換する。

    `query_params`はkintone_webhook.pyのクエリパラメータ方式の共有シークレット検証
    （`verify_webhook_query_param()`、kintoneのWebhook機能がカスタムHTTPヘッダーを
    送信できないための代替手段）で使う。他のハンドラは無視して構わない。
    """
    body = await request.body()
    return {
        "headers": dict(request.headers),
        "body": body.decode("utf-8"),
        "query_params": dict(request.query_params),
    }


def _partial_skip_summary(dispatcher: Any) -> list[dict[str, Any]] | None:
    """`SkipTrackingDispatcher.last_result`（直近のdispatch()結果）から、意図した書き込み先
    ツールのうち実際には反映されなかったものがあるプロパティの一覧を組み立てる。

    無ければNoneを返す（`dispatcher`がlast_resultを持たない場合・部分スキップが無い場合の
    いずれも含む）。obasan-quality/shirokuma-secレビュー: 「同期スキップが成功として見える」
    問題への対応として、warningログだけでなくWebhookレスポンス自体にも反映する。
    """
    last_result = getattr(dispatcher, "last_result", None)
    if last_result is None or not getattr(last_result, "has_partial_skips", False):
        return None
    return [
        {
            "property": p.property_name,
            "written_tools": sorted(t.value for t in p.written_tools),
            "skipped_tools": sorted(t.value for t in p.skipped_tools),
        }
        for p in last_result.properties
        if p.skipped_tools
    ]


def _record_authenticated_receipt(source: str, result: dict[str, Any]) -> None:
    """認証を通ったWebhookだけを「受信した」として記録する。

    購読が生きているかを後から判別するための記録（`src/sync_engine/webhook_receipts.py`）。
    **認証前に記録すると、外から適当なPOSTを1回投げるだけで「届いている」ように見えてしまい、
    購読が切れていても気づけないという、この機能が防ごうとしていた状態に戻る**
    （shirokuma-secレビューWARN、2026-08-31）。
    """
    if result.get("statusCode") == 401:
        return
    webhook_receipts.record_webhook_receipt(source)


# Webhook 専用の同時実行上限（ワーカースレッド本数）。
# anyio の共有スレッドプールは既定 40 本で、Webhook の再送バースト（2026-09-21 の Pub/Sub の
# 嵐のような状況）が 40 本を全部握ると、同じプールを使う他の同期処理（ダッシュボードの API、
# 同期 dependency）が空き待ちで巻き添えになる。イベントループを塞がなくなった代わりに
# 「プールを使い切る」という次の飽和点ができる、と Gemini・ChatGPT が独立に指摘
# （2026-09-24 他社レビュー）。超過分はここで**非同期に**待つだけなのでイベントループは
# 空いたまま。Vercel の 1 インスタンスで同時に本気で動かす Webhook が 8 本を超える状況は、
# そもそも送信元の再送を疑うべき異常なので、この値で足りる想定（実測での見直しは可）。
WEBHOOK_THREAD_LIMIT = 8
# CapacityLimiter はイベントループに紐づくので、ループごとに1つ作って使い回す
# （テストの TestClient はループを作り直すため、モジュール定数にはできない）。
_webhook_limiters: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, anyio.CapacityLimiter]" = (
    weakref.WeakKeyDictionary()
)


def _webhook_thread_limiter() -> anyio.CapacityLimiter:
    """現在のイベントループ用の Webhook 専用 CapacityLimiter を返す（無ければ作る）。"""
    loop = asyncio.get_running_loop()
    limiter = _webhook_limiters.get(loop)
    if limiter is None:
        limiter = anyio.CapacityLimiter(WEBHOOK_THREAD_LIMIT)
        _webhook_limiters[loop] = limiter
    return limiter


class _HandlerOutcome(NamedTuple):
    """`_run_off_event_loop` の戻り値。`partial_skip` は dispatcher を渡した経路だけ埋まる。"""

    result: dict[str, Any]
    partial_skip: list[dict[str, Any]] | None


async def _run_off_event_loop(
    handler: Callable[..., dict[str, Any]],
    event: dict[str, Any],
    *,
    receipt_source: str | None = None,
    dispatcher: Any = None,
    handler_kwargs: Mapping[str, Any] | None = None,
) -> _HandlerOutcome:
    """同期ハンドラをワーカースレッドで実行し、結果（Lambda 互換の dict）を返す。

    `handler_kwargs` はハンドラへそのまま渡すキーワード引数（`context=None` や wiring 由来の
    クライアント群）。`receipt_source` はこの関数自身が使う引数でハンドラには渡らない。
    2つを同じ並びに混ぜないのは、呼び出し箇所を読む人が「どれがハンドラの引数か」を
    迷わないようにするため（obasan-quality レビュー WARN 対応、2026-09-24）。

    各 Webhook のハンドラ本体は同期関数で、Notion・kintone・Zoho・Postgres を順に叩くため
    数秒〜数十秒、レート制限に当たれば分単位かかりうる。`async def` のルートの中で直接
    呼ぶとその間イベントループごと止まり、同じインスタンスに来た `/healthz` やダッシュボード
    の API まで全部待たされる（2026-09-21 の gmail-push 障害。40秒以上無応答になった）。
    gmail-push だけ先に直したが他の経路も同じ構造だったので（3レビュー共通指摘）、
    2026-09-24 に全経路をこの関数経由に揃えた。

    `receipt_source` を渡すと、認証を通った受信の記録（`_record_authenticated_receipt`、
    Postgres への書き込み）も同じワーカースレッドで済ませる。
    `dispatcher`（`SkipTrackingDispatcher`）を渡すと、部分スキップの要約
    （`_partial_skip_summary`）も**ハンドラと同じスレッドの中で**取り出す。dispatcher は
    プロセス内シングルトンで `last_result` はコンテキスト局所なので、イベントループに戻って
    から読むと別リクエストのコンテキストになり値が見えない／混ざる
    （shirokuma-sec レビュー WARN 対応、2026-09-24）。
    レスポンスを返した後に処理を続ける形（`BackgroundTasks`）にはしない — Vercel は
    レスポンス送信後にプロセスを凍結しうるため（`src/notifications/manager_dm.py` 参照）。

    スレッドは共有プールから借りるが、同時本数は `WEBHOOK_THREAD_LIMIT` の専用 limiter で
    絞る（共有 40 本を Webhook だけで使い切らないため）。`anyio.to_thread.run_sync` は
    呼び出しごとに contextvars を複製してスレッドへ渡す（Starlette の `run_in_threadpool`
    と同じ実体）。
    """

    def run() -> _HandlerOutcome:
        result = handler(event, **(handler_kwargs or {}))
        if receipt_source is not None:
            _record_authenticated_receipt(receipt_source, result)
        partial_skip = _partial_skip_summary(dispatcher) if dispatcher is not None else None
        return _HandlerOutcome(result, partial_skip)

    return await anyio.to_thread.run_sync(run, limiter=_webhook_thread_limiter())


def _lambda_result_to_response(
    result: dict[str, Any], *, partial_skip: list[dict[str, Any]] | None = None
) -> Response:
    """Webhookハンドラが返す`{"statusCode":..., "body":...}`をFastAPIの`Response`へ変換する。

    `partial_skip`（`_partial_skip_summary` の戻り値。`_run_off_event_loop` がハンドラと同じ
    スレッドで取り出したもの）があれば、意図した書き込み先ツールのうち実際には反映されなかった
    ものをレスポンスボディへ `partial_sync_skipped` フィールドとして追記する（ログだけでなく
    レスポンスからも後から追えるようにするため）。
    """
    body = result.get("body", "")
    if partial_skip is not None:
        try:
            body_data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            body_data = {}
        body_data["partial_sync_skipped"] = partial_skip
        body = json.dumps(body_data, ensure_ascii=False)
    return Response(
        content=body,
        status_code=result["statusCode"],
        media_type="application/json",
    )


# --- Webhook受信エンドポイント（リアルタイム連携） ------------------------------------------
# 認証は各handler内部の共有シークレット検証で行う（多くはX-Webhook-Secretヘッダー、
# src/sync_engine/webhook_handlers/_common.pyのverify_webhook_secret）。ただしZoho
# （カスタムヘッダー・bodyへの任意フィールド追加のいずれも不可、body内tokenフィールド方式、
# verify_webhook_body_token）・kintone（カスタムヘッダー不可、URLクエリパラメータ方式、
# verify_webhook_query_param）は、外部ツール側のWebhook機能の制約により別方式を使う。
#
# 2026-08-14時点の各ツール側Webhook購読登録状況: Zohoは本番登録済み・稼働中
# （docs/zoho_webhook_activation_note.md参照）。kintoneはkintone→Notion方向を有効化する
# 方針となり、本モジュール側の実装は完了（docs/kintone_webhook_activation_note.md参照）だが
# kintone管理画面側での購読登録はこの時点ではまだ手動作業が残っている。


@router.post("/api/webhooks/notion")
async def webhook_notion(
    request: Request, wiring: ProductionSyncWiring = Depends(wiring_dependency)
) -> Response:
    """Notion API Webhooksの受信エンドポイント。

    実際のNotion API Webhooksのペイロードはページ全体を含まないため、
    `handler_with_proxy()`（ページ全体をNotion APIから再取得するプロキシ層）を使う。
    """
    event = await _lambda_event_from_request(request)
    if wiring.any_db_page_client is None:
        logger.error(
            "webhook_notion: NOTION_API_KEY等が未設定のためNotion同期が構成されておらず、"
            "Webhookを処理できません"
        )
        return Response(
            content=json.dumps({"error": "notion sync is not configured"}),
            status_code=500,
            media_type="application/json",
        )
    outcome = await _run_off_event_loop(
        notion_webhook_handler_with_proxy,
        event,
        receipt_source=webhook_receipts.NOTION,
        dispatcher=wiring.dispatcher,
        handler_kwargs=dict(
            context=None,
            notion_client=wiring.any_db_page_client,
            dispatcher=wiring.dispatcher,
            calendar_sync=wiring.calendar_sync_callable,
            lead_sync=wiring.lead_sync_callable,
            project_mirror_sync=wiring.project_mirror_sync_callable,
            client_name_index_sync=wiring.client_name_index_sync_callable,
        ),
    )
    return _lambda_result_to_response(outcome.result, partial_skip=outcome.partial_skip)


@router.post("/api/webhooks/kintone")
async def webhook_kintone(
    request: Request, wiring: ProductionSyncWiring = Depends(wiring_dependency)
) -> Response:
    event = await _lambda_event_from_request(request)
    # id_mapping_store/notion_client: 取引先マスターリレーションの「後勝ち」上書き防止ガード用
    # （2026-08-25、GPT-5.6クロスレビュー指摘対応。kintone_webhook.pyのモジュールdocstring
    # 参照）。wiring.any_db_page_client未設定（NOTION_API_KEY未設定）の場合はNoneのまま渡され、
    # ガード自体が無効化される（kintone_webhook側は既存の挙動にフォールバックする）。
    outcome = await _run_off_event_loop(
        kintone_webhook_handler,
        event,
        receipt_source=webhook_receipts.KINTONE,
        dispatcher=wiring.dispatcher,
        handler_kwargs=dict(
            context=None,
            dispatcher=wiring.dispatcher,
            id_mapping_store=wiring.id_mapping_store,
            notion_client=wiring.any_db_page_client,
        ),
    )
    return _lambda_result_to_response(outcome.result, partial_skip=outcome.partial_skip)


@router.post("/api/webhooks/zoho")
async def webhook_zoho(
    request: Request, wiring: ProductionSyncWiring = Depends(wiring_dependency)
) -> Response:
    event = await _lambda_event_from_request(request)
    # id_mapping_store/notion_client/zoho_client: ⑥アクション履歴DBの取引先マスターリレーション
    # 自動解決・「後勝ち」上書き防止ガード用（2026-08-25、Round2。kintone側と同じ設計、
    # zoho_webhook.pyのモジュールdocstring参照）。wiring.any_db_page_client/zoho_action_client
    # が未設定（NOTION_API_KEY/Zoho認証情報未設定）の場合はNoneのまま渡され、当該機能自体が
    # 無効化される（zoho_webhook側は既存の挙動にフォールバックする）。
    outcome = await _run_off_event_loop(
        zoho_webhook_handler,
        event,
        receipt_source=webhook_receipts.ZOHO,
        dispatcher=wiring.dispatcher,
        handler_kwargs=dict(
            context=None,
            dispatcher=wiring.dispatcher,
            id_mapping_store=wiring.id_mapping_store,
            notion_client=wiring.any_db_page_client,
            zoho_client=wiring.zoho_action_client,
        ),
    )
    return _lambda_result_to_response(outcome.result, partial_skip=outcome.partial_skip)


@router.post("/api/webhooks/spreadsheet")
async def webhook_spreadsheet(
    request: Request, wiring: ProductionSyncWiring = Depends(wiring_dependency)
) -> Response:
    event = await _lambda_event_from_request(request)
    outcome = await _run_off_event_loop(
        spreadsheet_webhook_handler,
        event,
        receipt_source=webhook_receipts.SPREADSHEET,
        dispatcher=wiring.dispatcher,
        handler_kwargs=dict(context=None, dispatcher=wiring.dispatcher),
    )
    return _lambda_result_to_response(outcome.result, partial_skip=outcome.partial_skip)


@router.post("/api/webhooks/web-engagement")
async def webhook_web_engagement(request: Request) -> Response:
    """web-engagement-tool（別リポジトリ）からのリードのホットリード化・新規識別通知の受信。

    `Dispatcher`/`IdMappingStore`は経由しない設計（`web_engagement_webhook.handler`の
    docstring参照）のため、`_wiring_dependency`（Dispatcher一式）には依存しない。
    """
    event = await _lambda_event_from_request(request)
    outcome = await _run_off_event_loop(
        web_engagement_webhook_handler, event, handler_kwargs=dict(context=None)
    )
    return _lambda_result_to_response(outcome.result)


@router.post("/api/webhooks/web-engagement-meeting")
async def webhook_web_engagement_meeting(request: Request) -> Response:
    """web-engagement-tool（別リポジトリ）からのGoogleカレンダー商談イベント通知の受信。

    `Dispatcher`/`IdMappingStore`は経由しない設計（`web_engagement_meeting_webhook.handler`の
    docstring参照）のため、`_wiring_dependency`（Dispatcher一式）には依存しない。マッチした
    案件があればSlackへ承認依頼を投稿するのみで、この時点ではまだNotionへ書き込まない。
    """
    event = await _lambda_event_from_request(request)
    outcome = await _run_off_event_loop(
        web_engagement_meeting_webhook_handler, event, handler_kwargs=dict(context=None)
    )
    return _lambda_result_to_response(outcome.result)


@router.post("/api/webhooks/gmail-push")
async def webhook_gmail_push(request: Request) -> Response:
    """Google Cloud Pub/Subからの Gmail Push通知(`users.watch()`登録済みの新着メール検知)の
    受信エンドポイント。

    `Dispatcher`/`IdMappingStore`は経由しない設計(`gmail_push_webhook.handler`の
    docstring参照)のため、`_wiring_dependency`(Dispatcher一式)には依存しない。担当者が
    見つからない・処理中の例外いずれも、Pub/Subの再送ループを防ぐため常に200を返す。

    ハンドラ本体は Gmail API・Notion・DB を順に叩くため数十秒〜数分かかりうる。2026-09-21 の
    本番障害（イベントループを塞いで `/healthz` まで40秒以上無応答）を機に、ワーカースレッドで
    動かすようにした最初の経路。理由と方針は `_run_off_event_loop` の docstring に集約。"""
    event = await _lambda_event_from_request(request)
    outcome = await _run_off_event_loop(
        gmail_push_webhook_handler, event, handler_kwargs=dict(context=None)
    )
    return _lambda_result_to_response(outcome.result)


@router.post("/api/webhooks/lead-inquiry")
async def webhook_lead_inquiry(request: Request) -> Response:
    """lead-researcher（別リポジトリ、問い合わせメール自動調査Slackボット）からの
    リード情報受信。

    `Dispatcher`/`IdMappingStore`は経由しない設計（`lead_inquiry_webhook.handler`の
    docstring参照）のため、`_wiring_dependency`（Dispatcher一式）には依存しない。
    """
    event = await _lambda_event_from_request(request)
    outcome = await _run_off_event_loop(
        lead_inquiry_webhook_handler, event, handler_kwargs=dict(context=None)
    )
    return _lambda_result_to_response(outcome.result)


@router.post("/api/webhooks/slack-interactions")
async def webhook_slack_interactions(request: Request) -> Response:
    """Slack interactivity（承認/対象外ボタンの押下）の受信。

    `webhook_web_engagement_meeting`がSlackへ投稿した承認依頼メッセージへのコールバック。
    署名検証は共有トークン方式ではなくSlack標準の署名方式（`slack_interaction_webhook`
    内で実施）。承認時のみNotionアクション履歴DBへ実際に書き込む。
    """
    event = await _lambda_event_from_request(request)
    outcome = await _run_off_event_loop(
        slack_interaction_webhook_handler, event, handler_kwargs=dict(context=None)
    )
    return _lambda_result_to_response(outcome.result)
