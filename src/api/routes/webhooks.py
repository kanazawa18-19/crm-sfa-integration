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

import json
import logging
from typing import Any

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


def _lambda_result_to_response(result: dict[str, Any], *, dispatcher: Any = None) -> Response:
    """Webhookハンドラが返す`{"statusCode":..., "body":...}`をFastAPIの`Response`へ変換する。

    `dispatcher`（`SkipTrackingDispatcher`）を渡すと、直近のdispatch()で意図した書き込み先
    ツールのうち実際には反映されなかったものがあった場合、レスポンスボディへ
    `partial_sync_skipped`フィールドとして追記する（ログだけでなくレスポンスからも
    後から追えるようにするため）。
    """
    body = result.get("body", "")
    if dispatcher is not None:
        partial_skip = _partial_skip_summary(dispatcher)
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
    # 購読が生きているかを後から判別できるようにする（最善努力・失敗しても処理は続ける）。
    webhook_receipts.record_webhook_receipt(webhook_receipts.NOTION)
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
    result = notion_webhook_handler_with_proxy(
        event,
        context=None,
        notion_client=wiring.any_db_page_client,
        dispatcher=wiring.dispatcher,
        calendar_sync=wiring.calendar_sync_callable,
        lead_sync=wiring.lead_sync_callable,
        project_mirror_sync=wiring.project_mirror_sync_callable,
        client_name_index_sync=wiring.client_name_index_sync_callable,
    )
    return _lambda_result_to_response(result, dispatcher=wiring.dispatcher)


@router.post("/api/webhooks/kintone")
async def webhook_kintone(
    request: Request, wiring: ProductionSyncWiring = Depends(wiring_dependency)
) -> Response:
    # 購読が生きているかを後から判別できるようにする（最善努力・失敗しても処理は続ける）。
    webhook_receipts.record_webhook_receipt(webhook_receipts.KINTONE)
    event = await _lambda_event_from_request(request)
    # id_mapping_store/notion_client: 取引先マスターリレーションの「後勝ち」上書き防止ガード用
    # （2026-08-25、GPT-5.6クロスレビュー指摘対応。kintone_webhook.pyのモジュールdocstring
    # 参照）。wiring.any_db_page_client未設定（NOTION_API_KEY未設定）の場合はNoneのまま渡され、
    # ガード自体が無効化される（kintone_webhook側は既存の挙動にフォールバックする）。
    result = kintone_webhook_handler(
        event,
        context=None,
        dispatcher=wiring.dispatcher,
        id_mapping_store=wiring.id_mapping_store,
        notion_client=wiring.any_db_page_client,
    )
    return _lambda_result_to_response(result, dispatcher=wiring.dispatcher)


@router.post("/api/webhooks/zoho")
async def webhook_zoho(
    request: Request, wiring: ProductionSyncWiring = Depends(wiring_dependency)
) -> Response:
    # 購読が生きているかを後から判別できるようにする（最善努力・失敗しても処理は続ける）。
    webhook_receipts.record_webhook_receipt(webhook_receipts.ZOHO)
    event = await _lambda_event_from_request(request)
    # id_mapping_store/notion_client/zoho_client: ⑥アクション履歴DBの取引先マスターリレーション
    # 自動解決・「後勝ち」上書き防止ガード用（2026-08-25、Round2。kintone側と同じ設計、
    # zoho_webhook.pyのモジュールdocstring参照）。wiring.any_db_page_client/zoho_action_client
    # が未設定（NOTION_API_KEY/Zoho認証情報未設定）の場合はNoneのまま渡され、当該機能自体が
    # 無効化される（zoho_webhook側は既存の挙動にフォールバックする）。
    result = zoho_webhook_handler(
        event,
        context=None,
        dispatcher=wiring.dispatcher,
        id_mapping_store=wiring.id_mapping_store,
        notion_client=wiring.any_db_page_client,
        zoho_client=wiring.zoho_action_client,
    )
    return _lambda_result_to_response(result, dispatcher=wiring.dispatcher)


@router.post("/api/webhooks/spreadsheet")
async def webhook_spreadsheet(
    request: Request, wiring: ProductionSyncWiring = Depends(wiring_dependency)
) -> Response:
    # 購読が生きているかを後から判別できるようにする（最善努力・失敗しても処理は続ける）。
    webhook_receipts.record_webhook_receipt(webhook_receipts.SPREADSHEET)
    event = await _lambda_event_from_request(request)
    result = spreadsheet_webhook_handler(event, context=None, dispatcher=wiring.dispatcher)
    return _lambda_result_to_response(result, dispatcher=wiring.dispatcher)


@router.post("/api/webhooks/web-engagement")
async def webhook_web_engagement(request: Request) -> Response:
    """web-engagement-tool（別リポジトリ）からのリードのホットリード化・新規識別通知の受信。

    `Dispatcher`/`IdMappingStore`は経由しない設計（`web_engagement_webhook.handler`の
    docstring参照）のため、`_wiring_dependency`（Dispatcher一式）には依存しない。
    """
    event = await _lambda_event_from_request(request)
    result = web_engagement_webhook_handler(event, context=None)
    return _lambda_result_to_response(result)


@router.post("/api/webhooks/web-engagement-meeting")
async def webhook_web_engagement_meeting(request: Request) -> Response:
    """web-engagement-tool（別リポジトリ）からのGoogleカレンダー商談イベント通知の受信。

    `Dispatcher`/`IdMappingStore`は経由しない設計（`web_engagement_meeting_webhook.handler`の
    docstring参照）のため、`_wiring_dependency`（Dispatcher一式）には依存しない。マッチした
    案件があればSlackへ承認依頼を投稿するのみで、この時点ではまだNotionへ書き込まない。
    """
    event = await _lambda_event_from_request(request)
    result = web_engagement_meeting_webhook_handler(event, context=None)
    return _lambda_result_to_response(result)


@router.post("/api/webhooks/gmail-push")
async def webhook_gmail_push(request: Request) -> Response:
    """Google Cloud Pub/Subからの Gmail Push通知(`users.watch()`登録済みの新着メール検知)の
    受信エンドポイント。

    `Dispatcher`/`IdMappingStore`は経由しない設計(`gmail_push_webhook.handler`の
    docstring参照)のため、`_wiring_dependency`(Dispatcher一式)には依存しない。担当者が
    見つからない・処理中の例外いずれも、Pub/Subの再送ループを防ぐため常に200を返す。
    """
    event = await _lambda_event_from_request(request)
    result = gmail_push_webhook_handler(event, context=None)
    return _lambda_result_to_response(result)


@router.post("/api/webhooks/lead-inquiry")
async def webhook_lead_inquiry(request: Request) -> Response:
    """lead-researcher（別リポジトリ、問い合わせメール自動調査Slackボット）からの
    リード情報受信。

    `Dispatcher`/`IdMappingStore`は経由しない設計（`lead_inquiry_webhook.handler`の
    docstring参照）のため、`_wiring_dependency`（Dispatcher一式）には依存しない。
    """
    event = await _lambda_event_from_request(request)
    result = lead_inquiry_webhook_handler(event, context=None)
    return _lambda_result_to_response(result)


@router.post("/api/webhooks/slack-interactions")
async def webhook_slack_interactions(request: Request) -> Response:
    """Slack interactivity（承認/対象外ボタンの押下）の受信。

    `webhook_web_engagement_meeting`がSlackへ投稿した承認依頼メッセージへのコールバック。
    署名検証は共有トークン方式ではなくSlack標準の署名方式（`slack_interaction_webhook`
    内で実施）。承認時のみNotionアクション履歴DBへ実際に書き込む。
    """
    event = await _lambda_event_from_request(request)
    result = slack_interaction_webhook_handler(event, context=None)
    return _lambda_result_to_response(result)
