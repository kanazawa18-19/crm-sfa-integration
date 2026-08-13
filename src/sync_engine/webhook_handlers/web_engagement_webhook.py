"""web-engagement-tool Webhookの受信ハンドラ（M1: リードのホットリード化・新規識別の受信）。

web-engagement-tool側（別リポジトリ、CNCTOR JAPANのホテル/旅館向けオンサイトエンゲージメント
ツール）で、リードがホットリード化した・新規に識別された際、`X-Webhook-Secret`ヘッダー付きで
本エンドポイントへfire-and-forgetでプッシュされる想定（呼び出し元は範囲外）。

zoho_webhook.py/kintone_webhook.pyと同様にraw Lambda風の`handler(event, context, **kwargs)`
形状・`verify_webhook_secret()`による認証・`{"statusCode":..., "body": json}`の返却形式を
踏襲するが、他ハンドラと異なり`Dispatcher`/`IdMappingStore`は経由しない（設計方針:
この連携はAny-to-Any同期の汎用機構の外側で完結させる。web-engagement-tool側は
`Tool`/`SyncEvent`の対象外であり、Notion連絡先DBの`メールアドレス`のみを鍵とした
シンプルなupsertで十分なため）。そのため`dispatcher`ではなく`notion_client`
（`src/sync_engine/clients/notion_client.py`の`HttpNotionClient`を想定）を注入する。

想定ペイロード例（テストフィクスチャは tests/sync_engine/webhook_handlers/ を参照）:
{
  "event_type": "hot_lead",
  "lead_id": "lead_123",
  "email": "yamada@example.com",
  "company": "株式会社サンプル",
  "last_name": "山田",
  "first_name": "太郎",
  "phone": "090-1111-2222",
  "score": 82,
  "assigned_rep_email": "sales@cnctor.jp",
  "portal_url": "https://web-engagement-tool.example.com/leads/lead_123"
}

`phone`/`company`/`assigned_rep_email`は受け取っても意図的にNotionへ書き込まない。特に`phone`は
連絡先DBの`携帯番号`（sync_scope=ALL_TOOLS）に対応する既存プロパティが存在するが、そこへ書くと
実際のNotion API Webhook経由でdispatcher.dispatch()に届き、Notion発の変更として無条件で
Zoho/kintoneへ伝播してしまう（コンフリクト判定なし）。この連携のような未検証な外部入力を
ALL_TOOLS scopeのプロパティへ直接書いてはいけない（shirokuma-secレビューBLOCKER対応、
2026-08-13）。書き込むのは`リードスコア`/`ホットリード化日時`/`Web接客ツールURL`という、
この連携専用に追加したNOTION_ONLYプロパティのみに限定する。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol

from src.db_schema.registry import get_schema
from src.sync_engine.clients.notion_client import HttpNotionClient
from src.sync_engine.clients.notion_lookup import find_page_id_by_email
from src.sync_engine.webhook_handlers._common import (
    bad_request_response,
    internal_error_response,
    logger,
    unauthorized_response,
    verify_webhook_secret,
)

_DB_KEY = "contact"

_NAME_PROPERTY = "名前"
_EMAIL_PROPERTY = "メールアドレス"
_SCORE_PROPERTY = "リードスコア"
_HOT_LEAD_AT_PROPERTY = "ホットリード化日時"
_PORTAL_URL_PROPERTY = "Web接客ツールURL"

_HOT_LEAD_EVENT_TYPE = "hot_lead"


class ContactNotionClient(Protocol):
    """本ハンドラが連絡先DBに対して必要とする`NotionClient`の最小インターフェース。

    `query_all_pages()`の`filter`は省略可（`HttpNotionClient.query_all_pages()`のNotion
    Query Database APIフィルタ対応、WARN7対応）。テスト用Fakeが`filter`を受け取らず
    無視しても、`notion_lookup.find_page_id_by_email()`側のクライアント側フィルタで
    最終的な突合結果は変わらないため後方互換。
    """

    def query_all_pages(self, *, filter: dict[str, Any] | None = None) -> list[dict[str, Any]]: ...

    def create_page(self, properties: dict[str, Any]) -> str: ...

    def update_page(self, page_id: str, properties: dict[str, Any]) -> None: ...


def _default_notion_client() -> ContactNotionClient:
    schema = get_schema(_DB_KEY)
    return HttpNotionClient(_DB_KEY, schema.notion_database_id)


def _build_display_name(payload: Mapping[str, Any], email: str) -> str:
    """新規連絡先作成時の`名前`（TITLE、必須）を組み立てる。

    既存の他ソース（`src/migration/zoho_contact.py`等）に合わせ、`名前`には氏名のみを
    設定する（会社名は混入させない）。会社は本来`取引先マスター`リレーション
    （`src/db_schema/contact.py`）で持つ設計だが、この連携では`company`からの自動解決
    （企業名→取引先マスターページの名寄せ）までは実装しないため、`取引先マスター`は
    空のままにする。氏名が両方とも無い場合はemailをフォールバックとして使う。
    """
    last_name = (payload.get("last_name") or "").strip()
    first_name = (payload.get("first_name") or "").strip()
    name_part = f"{last_name}{first_name}"
    if name_part:
        return name_part
    return email


def handler(
    event: Mapping[str, Any], context: object, *, notion_client: ContactNotionClient | None = None
) -> dict[str, Any]:
    """Lambda/Cloud Functions エントリポイント（API Gateway形式のHTTPイベントを想定）。

    `notion_client`未注入時は環境変数（`NOTION_API_KEY`等）から本番用の
    `HttpNotionClient`を構築する（`notion_client`を渡すのはテスト時のみを想定）。
    """
    headers = event.get("headers") or {}
    if not verify_webhook_secret(headers, "WEB_ENGAGEMENT_WEBHOOK_SECRET"):
        return unauthorized_response()

    try:
        body = event.get("body")
        payload = json.loads(body) if isinstance(body, str) else (body or {})
        email = payload.get("email")
        if not isinstance(email, str) or not email.strip():
            raise ValueError("payload.email is required and must be a non-empty string")
        email = email.strip()
    except json.JSONDecodeError as exc:
        return bad_request_response(f"invalid JSON payload: {exc}")
    except ValueError as exc:
        return bad_request_response(str(exc))

    event_type = payload.get("event_type")

    try:
        client = notion_client if notion_client is not None else _default_notion_client()
        existing_page_id = find_page_id_by_email(client, _EMAIL_PROPERTY, email)

        properties: dict[str, Any] = {}
        score = payload.get("score")
        if score is not None:
            properties[_SCORE_PROPERTY] = score
        portal_url = payload.get("portal_url")
        if portal_url:
            properties[_PORTAL_URL_PROPERTY] = portal_url
        # payload.phoneは受け取っても`携帯番号`へは書き込まない（shirokuma-secレビュー
        # BLOCKER対応、2026-08-13）。`携帯番号`はsync_scope=ALL_TOOLSのため、Notionへの
        # 書き込みは実際のNotion API Webhook経由でdispatcher.dispatch()へ届く。Notion発の
        # 変更は常にマスターとして無条件伝播される設計（コンフリクト判定なし、
        # dispatcher.pyのNotion発イベント処理を参照）のため、このWebhookのように未検証な
        # 入力（web-engagement-tool側フォーム等）をそのまま書くと、Zoho/kintone側で
        # 営業担当が管理している正しい電話番号を無条件で上書きしてしまう。この連携が
        # 「Any-to-Any同期の汎用機構の外側で完結させる」（本モジュールdocstring参照）
        # という設計意図を保つには、ALL_TOOLS scopeのプロパティをここから書いてはいけない。
        # payload.assigned_rep_emailは受け取っても書き込まない。連絡先DB
        # （src/db_schema/contact.py）には担当営業に相当するプロパティが現状存在しない
        # ため、対応する受け皿が無く今回は捨てる（WARN3、2026-08-13）。
        if event_type == _HOT_LEAD_EVENT_TYPE:
            # 繰り返しのhot_lead通知で上書きされ続けるのは許容し、最新のホットリード化日時
            # として扱う（2026-08-13、金沢さん要望）。
            properties[_HOT_LEAD_AT_PROPERTY] = datetime.now(timezone.utc).isoformat()

        if existing_page_id is not None:
            if properties:
                client.update_page(existing_page_id, properties)
            page_id = existing_page_id
            created = False
        else:
            create_properties = dict(properties)
            create_properties[_NAME_PROPERTY] = _build_display_name(payload, email)
            create_properties[_EMAIL_PROPERTY] = email
            page_id = client.create_page(create_properties)
            created = True
    except Exception:
        logger.exception(
            "unexpected error while syncing web-engagement-tool lead to Notion contact db"
        )
        return internal_error_response()

    return {
        "statusCode": 200,
        "body": json.dumps({"page_id": page_id, "created": created}),
    }
