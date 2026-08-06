"""Notion Webhookの受信ハンドラ（05_同期・競合制御「変更検知の仕組み」: Notion API Webhooks）。

実際のNotion API Webhooksは変更されたプロパティIDのみを通知しページ全体は含まないため、
Webhook受信〜下記の想定ペイロード形式への整形の間にプロキシ層を挟む必要がある
（本モジュールの`handler()`はそのプロキシ後の形式を前提とする）。このプロキシ層は
`fetch_and_normalize_notion_page()`と`handler_with_proxy()`として実装済み（詳細は
docs/notion_webhook_proxy_note.md も参照）。実運用のエントリポイントとしては
`handler_with_proxy()`（実際の軽量Webhookペイロードを受け取る）を使う想定で、
`handler()`（整形済みペイロード前提）はテスト・段階的移行用に残している。

想定ペイロード例（テストフィクスチャは tests/sync_engine/webhook_handlers/ を参照）:
{
  "event_id": "evt_xxx",
  "type": "page.updated",
  "page_id": "26d6f1e2-0000-0000-0000-000000000000",
  "database_id": "26d6f1e2-1111-1111-1111-111111111111",
  "last_edited_time": "2026-08-05T09:00:00.000Z",
  "properties": {
    "案件ID": {"type": "title", "title": [{"plain_text": "MSA-PJ-001"}]},
    "営業ステータス": {"type": "status", "status": {"name": "提案中"}},
    "初期費用（イニシャル）": {"type": "number", "number": 500000}
  }
}

実際のNotion API Webhooksの軽量ペイロード例（`handler_with_proxy()`が受け取る形式）:
{
  "id": "evt_xxx",
  "timestamp": "2026-08-05T09:00:00.000Z",
  "workspace_id": "...",
  "type": "page.properties_updated",
  "entity": {"id": "26d6f1e2-0000-0000-0000-000000000000", "type": "page"},
  "data": {
    "parent": {"id": "26d6f1e2-1111-1111-1111-111111111111", "type": "database"},
    "updated_properties": ["title"]
  }
}
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Protocol

from src.db_schema.base import Tool
from src.sync_engine.clients._http import ApiError
from src.sync_engine.dispatcher import Dispatcher, DispatchResult
from src.sync_engine.sync_event import SyncEvent
from src.sync_engine.sync_headers import HEADER_NAME
from src.sync_engine.webhook_handlers._common import (
    bad_request_response,
    get_header,
    internal_error_response,
    logger,
    parse_iso_datetime,
    unauthorized_response,
    verify_webhook_secret,
)

# scripts/setup_notion_databases.py が作成時に書き出すキャッシュ（db_key -> notion database_id）。
# デフォルトの逆引き元として利用する（テスト等では db_id_to_db_key を明示的に注入する）。
_DEFAULT_DB_IDS_CACHE_PATH = (
    Path(__file__).resolve().parents[3] / "scripts" / ".notion_db_ids.json"
)


def _default_db_id_to_db_key() -> dict[str, str]:
    if not _DEFAULT_DB_IDS_CACHE_PATH.exists():
        return {}
    raw: dict[str, str] = json.loads(_DEFAULT_DB_IDS_CACHE_PATH.read_text(encoding="utf-8"))
    return {database_id: db_key for db_key, database_id in raw.items()}


def parse_notion_property_value(prop: Mapping[str, Any]) -> Any:
    """Notion APIのプロパティ値オブジェクトを素のPython値へ変換する。"""
    prop_type = prop.get("type")
    if prop_type in ("title", "rich_text"):
        parts = prop.get(prop_type) or []
        text = "".join(part.get("plain_text", "") for part in parts)
        return text or None
    if prop_type == "select":
        select = prop.get("select")
        return select.get("name") if select else None
    if prop_type == "status":
        status = prop.get("status")
        return status.get("name") if status else None
    if prop_type == "number":
        return prop.get("number")
    if prop_type == "checkbox":
        return prop.get("checkbox")
    if prop_type == "date":
        date = prop.get("date")
        return date.get("start") if date else None
    if prop_type in ("email", "phone_number", "url"):
        return prop.get(prop_type)
    if prop_type == "relation":
        return [item["id"] for item in prop.get("relation") or []]
    if prop_type == "people":
        return [person.get("id") for person in prop.get("people") or []]
    raise ValueError(f"unsupported Notion property type: {prop_type!r}")


def notion_payload_to_sync_event(
    payload: Mapping[str, Any],
    headers: Mapping[str, str],
    *,
    db_id_to_db_key: Mapping[str, str] | None = None,
) -> SyncEvent:
    """Notion Webhookペイロードを共通のSyncEventへ変換する。"""
    resolver = db_id_to_db_key if db_id_to_db_key is not None else _default_db_id_to_db_key()
    database_id = payload["database_id"]
    db_key = resolver.get(database_id)
    if db_key is None:
        raise ValueError(f"unknown Notion database_id: {database_id!r}")

    properties = {
        name: parse_notion_property_value(value)
        for name, value in (payload.get("properties") or {}).items()
    }

    return SyncEvent(
        source_tool=Tool.NOTION,
        db_key=db_key,
        external_id=payload["page_id"],
        occurred_at=parse_iso_datetime(payload["last_edited_time"]),
        properties=properties,
        sync_system_id=get_header(headers, HEADER_NAME),
    )


class NotionPageClient(Protocol):
    """Notion API `GET /v1/pages/{page_id}` の生レスポンスをそのまま返す最小インターフェース。"""

    def get_raw_page(self, page_id: str) -> Mapping[str, Any]: ...


def fetch_and_normalize_notion_page(page_id: str, notion_client: NotionPageClient) -> dict[str, Any]:
    """Notion APIからページ全体を再取得し、本モジュールが期待するペイロード形式
    （{"page_id", "database_id", "last_edited_time", "properties"}）へ整形する。

    実際のNotion API Webhooksは変更されたプロパティIDのみを通知しページ全体は含まないため、
    Webhook受信〜handler()呼び出しの間のプロキシ層（`handler_with_proxy()`）がこの関数を
    利用する。notion_client.get_raw_page()の返り値はNotion API `GET /v1/pages/{id}`の
    レスポンス形式（id / parent.database_id / last_edited_time / properties を含む）を想定する。
    """
    page = notion_client.get_raw_page(page_id)
    parent = page.get("parent") or {}
    return {
        "page_id": page["id"],
        "database_id": parent.get("database_id"),
        "last_edited_time": page["last_edited_time"],
        "properties": dict(page.get("properties") or {}),
    }


def handler(
    event: Mapping[str, Any], context: object, *, dispatcher: Dispatcher | None = None
) -> dict[str, Any]:
    """整形済みペイロード（page_id/database_id/last_edited_time/propertiesを含む形式）を
    受け取るエントリポイント。テスト・段階的移行用に残しており、実運用では
    `handler_with_proxy()`を使うこと（実際のNotion API Webhooksのペイロードは
    ページ全体を含まないため、本handler()単体では利用できない）。

    dispatcherを注入すれば変換後のSyncEventをそのままディスパッチする
    （未注入時は変換結果の検証のみ行う）。
    """
    headers = event.get("headers") or {}
    if not verify_webhook_secret(headers, "NOTION_WEBHOOK_SECRET"):
        return unauthorized_response()

    try:
        body = event.get("body")
        payload = json.loads(body) if isinstance(body, str) else (body or {})
        sync_event = notion_payload_to_sync_event(payload, headers)
    except json.JSONDecodeError as exc:
        return bad_request_response(f"invalid JSON payload: {exc}")
    except (KeyError, ValueError) as exc:
        return bad_request_response(str(exc))
    except Exception:
        logger.exception("unexpected error while parsing notion webhook payload")
        return internal_error_response()

    try:
        result: DispatchResult | None = (
            dispatcher.dispatch(sync_event) if dispatcher is not None else None
        )
    except Exception:
        logger.exception("unexpected error while dispatching notion sync event")
        return internal_error_response()

    return {
        "statusCode": 200,
        "body": json.dumps({"skipped": result.skipped if result is not None else None}),
    }


def handler_with_proxy(
    event: Mapping[str, Any],
    context: object,
    *,
    notion_client: NotionPageClient,
    dispatcher: Dispatcher | None = None,
) -> dict[str, Any]:
    """Lambda/Cloud Functions エントリポイント（実際のNotion API Webhooksの軽量ペイロードを
    受け取る想定。API Gateway形式のHTTPイベントを想定）。実運用ではこちらを使う。

    実際のNotion API Webhooksは変更されたプロパティIDのみを通知しページ全体は含まないため、
    軽量ペイロードから`entity.id`（page_id）を取り出し、`notion_client.get_raw_page()`で
    ページ全体を再取得・`fetch_and_normalize_notion_page()`で整形してから`handler()`相当の
    処理（notion_payload_to_sync_event -> dispatch）を行う。

    ページが既に削除されている等でNotion APIが404を返した場合は、Notion Webhooksの
    再送仕様により500を返し続けると無駄な再送ループになりうるため、200＋
    `{"skipped": "page_not_found"}`で応答する（削除イベント自体の同期先への伝播は
    SyncEvent/Dispatcher側が未対応のため範囲外）。

    実際のデプロイ設定（SAM/Serverless Framework等）は範囲外。dispatcherを注入すれば
    変換後のSyncEventをそのままディスパッチする（未注入時は変換結果の検証のみ行う）。
    """
    headers = event.get("headers") or {}
    if not verify_webhook_secret(headers, "NOTION_WEBHOOK_SECRET"):
        return unauthorized_response()

    try:
        body = event.get("body")
        raw_payload = json.loads(body) if isinstance(body, str) else (body or {})
        page_id = raw_payload["entity"]["id"]
    except json.JSONDecodeError as exc:
        return bad_request_response(f"invalid JSON payload: {exc}")
    except (KeyError, TypeError) as exc:
        return bad_request_response(f"missing required field: {exc}")

    try:
        payload = fetch_and_normalize_notion_page(page_id, notion_client)
    except ApiError as exc:
        if exc.status_code == 404:
            logger.info(
                "notion page not found (likely deleted), skipping webhook: page_id=%s", page_id
            )
            return {"statusCode": 200, "body": json.dumps({"skipped": "page_not_found"})}
        logger.exception(
            "notion api error while fetching notion page for webhook proxy: page_id=%s", page_id
        )
        return internal_error_response()
    except Exception:
        logger.exception(
            "unexpected error while fetching notion page for webhook proxy: page_id=%s", page_id
        )
        return internal_error_response()

    try:
        sync_event = notion_payload_to_sync_event(payload, headers)
    except (KeyError, ValueError) as exc:
        return bad_request_response(str(exc))
    except Exception:
        logger.exception("unexpected error while parsing notion webhook payload")
        return internal_error_response()

    try:
        result: DispatchResult | None = (
            dispatcher.dispatch(sync_event) if dispatcher is not None else None
        )
    except Exception:
        logger.exception("unexpected error while dispatching notion sync event")
        return internal_error_response()

    return {
        "statusCode": 200,
        "body": json.dumps({"skipped": result.skipped if result is not None else None}),
    }
