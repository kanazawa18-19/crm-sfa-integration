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
import os
from typing import Any, Callable, Collection, Mapping, Protocol

from src.db_schema.base import PropertyType, Tool
from src.db_schema.registry import ALL_SCHEMAS, get_schema
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


def _default_db_id_to_db_key() -> dict[str, str]:
    """DBスキーマ定義（src.db_schema.registry.ALL_SCHEMAS）から notion database_id -> db_key
    の逆引き表を直接組み立てる。以前は scripts/.notion_db_ids.json キャッシュファイルを
    読み込んでいたが、全6DBが既に DatabaseSchema.notion_database_id を保持しているため
    キャッシュは不要になった（shirokuma-secレビュー: WARN）。デフォルトの逆引き元として
    利用する（テスト等では db_id_to_db_key を明示的に注入する）。
    """
    return {
        schema.notion_database_id: schema.key
        for schema in ALL_SCHEMAS
        if schema.notion_database_id is not None
    }


# parse_notion_property_value()が実際にパース可能なスキーマ上のプロパティ型のホワイトリスト。
# is_writable（Notion API上書き込み可能か）だけで判定すると、FILESのように書き込み可能
# だがparse_notion_property_value()が未対応の型が素通りしてValueErrorを送出してしまう
# （案件管理DBの「申込書・契約書」「見積書」がFILES型かつsync_scope=NOTION_ONLYで実在する）。
# 将来Notion側に新しい型が追加された場合も、ここに無ければ安全側（スキップ）に倒れる。
_SYNCABLE_PROPERTY_TYPES: frozenset[PropertyType] = frozenset(
    {
        PropertyType.TITLE,
        PropertyType.TEXT,
        PropertyType.SELECT,
        PropertyType.STATUS,
        PropertyType.NUMBER,
        PropertyType.CURRENCY,
        PropertyType.CHECKBOX,
        PropertyType.DATE,
        PropertyType.DATETIME,
        PropertyType.EMAIL,
        PropertyType.PHONE,
        PropertyType.URL,
        PropertyType.RELATION,
        PropertyType.USER,
        PropertyType.MULTI_SELECT,
    }
)


# parse_notion_property_value()が実際にif分岐でswitchしているNotion API上の生の
# プロパティ型文字列（`type`フィールドの値）の一覧。`_SYNCABLE_PROPERTY_TYPES`
# （このコードベース独自のPropertyType Enum値）とは1対1で対応しない
# （例: PropertyType.TEXT="text"だがNotion APIの生の型文字列は"rich_text"）ため、
# 別に定義する。以前は`src/sync_engine/clients/notion_client.py`側にも同じ内容を
# 重複定義していたが、parse_notion_property_value()が対応する型が増減した際に
# 片方だけ更新漏れが起きるとクラッシュ(whitelistが狭すぎる)または元のバグ
# (whitelistが広すぎて未対応型を渡してしまう)が再発するため、ここを唯一の
# 情報源とし`notion_client.py`側からimportして使う。値を変更する場合は
# 必ず下記のif分岐（parse_notion_property_value()）も合わせて更新すること
# （`tests/sync_engine/webhook_handlers/test_notion_webhook.py`の
# `test_parseable_notion_property_types_matches_parse_notion_property_value_branches`
# がこの2つのズレを検知する）。
PARSEABLE_NOTION_PROPERTY_TYPES: frozenset[str] = frozenset(
    {
        "title",
        "rich_text",
        "select",
        "status",
        "multi_select",
        "number",
        "checkbox",
        "date",
        "email",
        "phone_number",
        "url",
        "relation",
        "people",
    }
)


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
    if prop_type == "multi_select":
        return [option.get("name") for option in prop.get("multi_select") or []]
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

    schema = get_schema(db_key)
    properties: dict[str, Any] = {}
    for name, value in (payload.get("properties") or {}).items():
        try:
            prop_def = schema.get_property(name)
        except KeyError:
            # Notion側で列が将来追加された場合等にWebhook処理全体を落とさないよう無視する。
            logger.warning(
                "ignoring unknown Notion property '%s' for db_key=%r (not in schema)",
                name,
                db_key,
            )
            continue
        if prop_def.property_type not in _SYNCABLE_PROPERTY_TYPES:
            # parse_notion_property_value()が未対応の型（files等）はホワイトリスト外として
            # スキップする。is_writable（Notion API上書き込み可能か）だけでは判定できない
            # （FILESは書き込み可能だがparse_notion_property_value()が非対応）。
            logger.debug(
                "skipping non-syncable Notion property '%s' (type=%s) for db_key=%r",
                name,
                prop_def.property_type.value,
                db_key,
            )
            continue
        if not prop_def.is_writable:
            # rollup/formula/button/unique_id/created_time/last_edited_time/created_by等は
            # sync_scope=INTERNALで元々同期対象外。
            logger.debug(
                "skipping read-only Notion property '%s' (type=%s) for db_key=%r",
                name,
                prop_def.property_type.value,
                db_key,
            )
            continue
        properties[name] = parse_notion_property_value(value)

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


#: 同期エンジン自身のNotionインテグレーション（bot）のユーザーID。
#: `GET /v1/users/me` の `id`（本番は "3b4d8ea8-d4f3-81ee-b550-0027586fe38e"）。
NOTION_SYNC_BOT_ID_ENV_VAR = "NOTION_SYNC_BOT_ID"


def _page_last_edited_by(page: Mapping[str, Any]) -> str:
    editor = page.get("last_edited_by")
    return str(editor.get("id") or "") if isinstance(editor, Mapping) else ""


def is_own_notion_write(page: Mapping[str, Any]) -> bool | None:
    """このページ更新が「同期エンジン自身の書き込み」かどうか。判定できなければNone。

    ■ なぜヘッダーではなくページの最終更新者で見るのか（2026-08-31）

    無限ループ防止は本来 `X-Sync-System-ID` ヘッダーで行っている
    （`dispatcher.dispatch()` の先頭）。ところが **Notion の Webhook は
    カスタムヘッダーを送れない**ので、Notion発のイベントにはこの仕組みが効かない。
    そのため購読を作った瞬間、次の反射が生まれる。

        Zoho変更 → Zoho Webhook → Notion更新 → Notion Webhook → Zoho更新 → …

    値が同じなら競合解決が NO_OP に倒れて止まるが、**取り込み時に値が正規化される項目
    （「株式会社ABC」→「ABC」等）では、元データを書き換えてしまう**
    （2026-08-31、ChatGPT・Geminiが独立に指摘）。

    外部発のイベントは1回のdispatchでNotion・kintone・スプレッドシートへ**まとめて**
    書き込まれる（`Dispatcher._write_values`）。つまり自分の書き込みが返ってきた
    Webhookには、他ツールへ伝えるべき新しい情報が何も無い。丸ごと捨ててよい。
    """
    bot_id = os.environ.get(NOTION_SYNC_BOT_ID_ENV_VAR, "").strip()
    if not bot_id:
        return None
    return _page_last_edited_by(page) == bot_id


def fetch_and_normalize_notion_page(
    page_id: str,
    notion_client: NotionPageClient,
    updated_property_ids: Collection[str] | None = None,
) -> dict[str, Any]:
    """Notion APIからページ全体を再取得し、本モジュールが期待するペイロード形式
    （{"page_id", "database_id", "last_edited_time", "properties"}）へ整形する。

    実際のNotion API Webhooksは変更されたプロパティIDのみを通知しページ全体は含まないため、
    Webhook受信〜handler()呼び出しの間のプロキシ層（`handler_with_proxy()`）がこの関数を
    利用する。notion_client.get_raw_page()の返り値はNotion API `GET /v1/pages/{id}`の
    レスポンス形式（id / parent.database_id / last_edited_time / properties を含む）を想定する。

    ■ `updated_property_ids`（2026-08-31、ChatGPTクロスレビューBLOCKER対応）

    **Webhookが「変更されたプロパティのID」を教えてくれるので、そこへ絞る。**
    ページ全体を渡すと、実際には触られていない項目まで外部ツールへ伝播対象になる。

        14:00     Zohoで電話番号を変更
        14:00:01  Notionで案件名だけを変更
        14:00:02  Notionページ全体をZohoへ → **Notionに残っていた古い電話番号で上書き**

    これは「テストは通るが本番データだけ壊れる」形の事故。名前ではなくNotionの
    プロパティIDで突き合わせる（プロパティ名の変更は表示上の操作でも、同期にとっては
    別物になるため）。

    Noneのとき（ページ作成イベント・IDが取れなかった場合）は全項目を返す。
    絞った結果1件も残らない場合も全項目を返す（IDの形式が想定と違うときに、
    何も同期されない状態へ静かに倒れるのを避ける）。
    """
    return _normalize_fetched_page(notion_client.get_raw_page(page_id), updated_property_ids)


def _normalize_fetched_page(
    page: Mapping[str, Any], updated_property_ids: Collection[str] | None = None
) -> dict[str, Any]:
    """取得済みのページを整形する（`fetch_and_normalize_notion_page`の本体）。

    `handler_with_proxy()`は「自分の書き込みか」を判定するために先にページを取得するので、
    二度取りしないようここだけ分けている。
    """
    page_id = page.get("id")
    parent = page.get("parent") or {}
    properties = dict(page.get("properties") or {})
    if updated_property_ids:
        wanted = set(updated_property_ids)
        changed = {
            name: value
            for name, value in properties.items()
            if isinstance(value, Mapping) and value.get("id") in wanted
        }
        if changed:
            properties = changed
        else:
            logger.warning(
                "notion webhook: updated_properties=%r に一致するプロパティがページ側に"
                "見つからなかったため、全項目を対象にします (page_id=%s)",
                sorted(wanted),
                page_id,
            )
    return {
        "page_id": page["id"],
        "database_id": parent.get("database_id"),
        "last_edited_time": page["last_edited_time"],
        "properties": properties,
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
    calendar_sync: Callable[[Mapping[str, Any], str], Any] | None = None,
    lead_sync: Callable[[Mapping[str, Any], str], Any] | None = None,
    project_mirror_sync: Callable[[Mapping[str, Any], str], Any] | None = None,
    client_name_index_sync: Callable[[Mapping[str, Any], str], Any] | None = None,
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

    `calendar_sync`（省略可、既定`None`）を注入すると、db_key="project"（案件管理DB）の
    SyncEventについて`calendar_sync(sync_event.properties, sync_event.external_id)`を呼び、
    「次回アクション日」変更をGoogle Calendarへ同期する
    （`src.calendar_sync.service.sync_next_action_date_to_calendar`を想定）。

    `lead_sync`（省略可、既定`None`）を注入すると、db_key="contact"（連絡先DB）の
    SyncEventについて`lead_sync(sync_event.properties, sync_event.external_id)`を呼び、
    連絡先レコードをweb-engagement-tool側のLeadシステムへ同期する
    （`src.lead_sync.service.sync_contact_to_lead`を想定）。

    `project_mirror_sync`（省略可、既定`None`）を注入すると、db_key="project"（案件管理DB）の
    SyncEventについて`project_mirror_sync(sync_event.properties, sync_event.external_id)`を呼び、
    案件管理DBのPostgresミラー（`ProjectMirror`）を更新する
    （`src.project_mirror.sync.sync_project_to_mirror`を想定、2026-08-17）。

    `client_name_index_sync`（省略可、既定`None`）を注入すると、db_key="client_master"
    （取引先マスターDB）のSyncEventについて
    `client_name_index_sync(sync_event.properties, sync_event.external_id)`を呼び、
    取引先マスターDBの正規化取引先名→Notion page IDインデックス（`ClientNameIndex`）を
    更新する（`src.relation_sync.sync.sync_client_name_to_index`を想定、2026-08-25。
    `src.relation_sync.resolve.resolve_client_master_relation`がkintone等からのリレーション
    解決に使う）。

    いずれも`dispatcher.dispatch()`の同期処理とは独立した副作用であり、例外を送出しても
    Webhook全体としては既存の200レスポンスをそのまま返す（メインの同期処理を絶対に
    壊さないため）。
    """
    headers = event.get("headers") or {}
    if not verify_webhook_secret(headers, "NOTION_WEBHOOK_SECRET"):
        return unauthorized_response()

    try:
        body = event.get("body")
        raw_payload = json.loads(body) if isinstance(body, str) else (body or {})
        page_id = raw_payload["entity"]["id"]
        # 変更されたプロパティIDだけへ絞る（上記docstring参照）。
        updated_property_ids = (raw_payload.get("data") or {}).get("updated_properties") or None
    except json.JSONDecodeError as exc:
        return bad_request_response(f"invalid JSON payload: {exc}")
    except (KeyError, TypeError) as exc:
        return bad_request_response(f"missing required field: {exc}")

    try:
        raw_page = notion_client.get_raw_page(page_id)
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

    own_write = is_own_notion_write(raw_page)
    if own_write:
        logger.info(
            "notion webhook: 同期エンジン自身の書き込みによる通知なのでスキップします "
            "(page_id=%s)",
            page_id,
        )
        return {"statusCode": 200, "body": json.dumps({"skipped": "own_system_write"})}
    if own_write is None:
        # **判定材料が無いまま処理すると同期ループを起こす。** 書かない側へ倒す。
        logger.error(
            "notion webhook: %s が未設定のため、自分の書き込みかどうかを判定できません。"
            "同期ループを避けるため、このイベントは処理しません (page_id=%s)",
            NOTION_SYNC_BOT_ID_ENV_VAR,
            page_id,
        )
        return {
            "statusCode": 200,
            "body": json.dumps({"skipped": "sync_bot_id_not_configured"}),
        }

    try:
        payload = _normalize_fetched_page(raw_page, updated_property_ids)
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

    if calendar_sync is not None and sync_event.db_key == "project":
        try:
            calendar_sync(sync_event.properties, sync_event.external_id)
        except Exception:
            logger.exception(
                "unexpected error while syncing calendar event (non-fatal, "
                "webhook still returns 200): page_id=%s",
                sync_event.external_id,
            )

    if project_mirror_sync is not None and sync_event.db_key == "project":
        try:
            project_mirror_sync(sync_event.properties, sync_event.external_id)
        except Exception:
            logger.exception(
                "unexpected error while syncing project mirror (non-fatal, "
                "webhook still returns 200): page_id=%s",
                sync_event.external_id,
            )

    if client_name_index_sync is not None and sync_event.db_key == "client_master":
        try:
            client_name_index_sync(sync_event.properties, sync_event.external_id)
        except Exception:
            logger.exception(
                "unexpected error while syncing client name index (non-fatal, "
                "webhook still returns 200): page_id=%s",
                sync_event.external_id,
            )

    if lead_sync is not None and sync_event.db_key == "contact":
        try:
            lead_sync(sync_event.properties, sync_event.external_id)
        except Exception as exc:
            # shirokuma-secレビューWARN対応（2026-08-13）: logger.exception()は例外メッセージ
            # 全文をログへ記録するが、LeadSyncApiError（`extract_error_message()`経由で
            # web-engagement-tool側のHTTPエラーレスポンス本文を最大200文字まで含みうる）の
            # 場合、相手先が不正な入力値をエラーメッセージへエコーバックする一般的なAPI
            # パターン（例: "invalid email: foo@bar"）により、連絡先のPII（メールアドレス等）が
            # このアプリの標準ログへ漏れる恐れがある（docs/migration_pipeline_note.md
            # 「6. PIIの取り扱い」の方針同様、PIIはリポジトリ管理下の専用出力先以外に出さない）。
            # 例外の型名・（ApiErrorサブクラスであれば）status_code・notion page_id
            # （内部識別子でありPIIではない）のみを記録し、メッセージ本文は記録しない。
            #
            # calendar_syncの同等の失敗ログ（上のブロック）は同じ理由で見直していない
            # （担当メンバーの社内メールアドレスしか扱わず、顧客の連絡先PIIを含まないため
            # 影響範囲が小さいと判断した。lead_syncのみを対象とする）。
            logger.error(
                "unexpected error while syncing lead (non-fatal, "
                "webhook still returns 200): page_id=%s exc_type=%s status_code=%s",
                sync_event.external_id,
                type(exc).__name__,
                getattr(exc, "status_code", None),
            )

    return {
        "statusCode": 200,
        "body": json.dumps({"skipped": result.skipped if result is not None else None}),
    }
