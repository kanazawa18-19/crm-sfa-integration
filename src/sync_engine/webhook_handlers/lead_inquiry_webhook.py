"""問い合わせをNotionへ空欄追記し、保存差分を永続台帳から明示的に同期する。

会社は完全一致の一意候補だけを採用する。会社未確定・姓未入力の新規CRM作成は保留し、
Notionの自己Webhookへ依存せず、再送時にも保存済みの未配送差分を引き継ぐ。
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Protocol

from src.audit_log.actor_context import set_actor
from src.hub_creation.inquiry_handoff import InquiryHandoff
from src.sync_engine.clients.notion_client import parse_notion_property_value
from src.db_schema.registry import get_schema
from src.sync_engine.clients.notion_client import HttpNotionClient
from src.sync_engine.webhook_handlers._common import (
    bad_request_response,
    internal_error_response,
    logger,
    unauthorized_response,
    verify_webhook_secret,
)

_CONTACT_DB_KEY = "contact"
_CLIENT_MASTER_DB_KEY = "client_master"

_NAME_PROPERTY = "名前"
_EMAIL_PROPERTY = "メールアドレス"
_PHONE_PROPERTY = "携帯番号"
_CLIENT_MASTER_RELATION_PROPERTY = "取引先マスター"
_CLIENT_MASTER_TITLE_PROPERTY = "取引先名"


class LeadInquiryContactClient(Protocol):
    """本ハンドラが連絡先DB/取引先マスターDBに対して必要とする`NotionClient`の最小
    インターフェース。`get_page`は「空欄なら埋める」判定のため既存プロパティ値を取得する
    のに使う（`web_engagement_webhook.py`の`ContactNotionClient`と異なり、更新前に
    現在値を読む必要があるため`get_page`を追加で要求する）。
    """

    def query_all_pages(self, *, filter: dict[str, Any] | None = None) -> list[dict[str, Any]]: ...

    def get_page(self, page_id: str) -> dict[str, Any] | None: ...

    def create_page(self, properties: dict[str, Any]) -> str: ...

    def update_page(self, page_id: str, properties: dict[str, Any]) -> None: ...


def _default_contact_client() -> LeadInquiryContactClient:
    schema = get_schema(_CONTACT_DB_KEY)
    return HttpNotionClient(_CONTACT_DB_KEY, schema.notion_database_id)


def _default_client_master_client() -> LeadInquiryContactClient:
    schema = get_schema(_CLIENT_MASTER_DB_KEY)
    return HttpNotionClient(_CLIENT_MASTER_DB_KEY, schema.notion_database_id)


def _exact_ids(client, property_name, value, kind):
    pages = client.query_all_pages(filter={'property': property_name, kind: {'equals': value.strip()}})
    expected = value.strip().lower() if kind == 'email' else value.strip()
    found = []
    for page in pages:
        prop = (page.get('properties') or {}).get(property_name)
        parsed = parse_notion_property_value(prop) if prop else None
        normalized = parsed.strip().lower() if isinstance(parsed, str) and kind == 'email' else parsed
        if normalized == expected and not page.get('archived') and not page.get('in_trash'):
            found.append(page['id'])
    return list(dict.fromkeys(found))


class AmbiguousContact(ValueError):
    pass


def _resolve_existing_contact(
    contact: LeadInquiryContactClient,
    *,
    email: str,
    name: str,
    client_master_page_id: str | None,
) -> tuple[str | None, dict[str, Any]]:
    """既存連絡先のページIDと（見つかった場合は）そのプロパティ値を返す。

    メールで見つからず名前フォールバックへ進む場合、同姓同名の別人を誤って同一人物と
    みなさないよう、`取引先マスター`が今回のcompanyと一致する場合のみ採用する
    （モジュールdocstring参照）。
    """
    if email:
        matches = _exact_ids(contact, _EMAIL_PROPERTY, email, 'email')
        if len(matches) > 1:
            raise AmbiguousContact('同じメールの連絡先が複数あります。統合せず確認を待ちます')
        if matches:
            current = contact.get_page(matches[0])
            if current is None:
                raise RuntimeError('既存連絡先の読戻しができません')
            return matches[0], current
    if name:
        matches = []
        named = _exact_ids(contact, _NAME_PROPERTY, name, 'title')
        for page_id in named:
            current = contact.get_page(page_id)
            if current is None:
                raise RuntimeError('既存連絡先の読戻しができません')
            if client_master_page_id and client_master_page_id in (current.get(_CLIENT_MASTER_RELATION_PROPERTY) or []):
                matches.append((page_id, current))
        if len(matches) > 1:
            raise AmbiguousContact('同じ会社と名前の連絡先が複数あります')
        if matches:
            return matches[0]
        if named:
            raise AmbiguousContact('同名の連絡先がありますが会社で一意に同定できません')

    return None, {}


def handler(
    event: Mapping[str, Any],
    context: object,
    *,
    contact_client: LeadInquiryContactClient | None = None,
    client_master_client: LeadInquiryContactClient | None = None,
    handoff=None,
) -> dict[str, Any]:
    """Lambda/Cloud Functions エントリポイント（API Gateway形式のHTTPイベントを想定）。"""
    headers = event.get("headers") or {}
    if not verify_webhook_secret(headers, "LEAD_RESEARCHER_WEBHOOK_SECRET"):
        return unauthorized_response()

    try:
        body = event.get("body")
        payload = json.loads(body) if isinstance(body, str) else (body or {})
    except json.JSONDecodeError as exc:
        return bad_request_response(f"invalid JSON payload: {exc}")

    # 構文的には正しいJSONでも辞書でない場合（例: "null"/"[1,2,3]"/"42"/"true"）、次の
    # payload.get()でAttributeErrorが未捕捉のまま外へ漏れてしまうため400へ倒す
    # （zoho_webhook.pyのBLOCKER2対応と同じガード、shirokuma-secレビューWARN対応）。
    if not isinstance(payload, dict):
        return bad_request_response("request body must be a JSON object")

    if any(payload.get(key) is not None and not isinstance(payload[key], str)
           for key in ('company', 'name', 'email', 'phone')):
        return bad_request_response('問い合わせ項目は文字列にしてください')
    company = (payload.get("company") or "").strip()
    name = (payload.get("name") or "").strip()
    email = (payload.get("email") or "").strip()
    phone = (payload.get("phone") or "").strip()
    if not email and not name:
        return bad_request_response("payload must include a non-empty 'email' or 'name'")

    try:
        with set_actor("lead_inquiry_webhook"):
            contact = contact_client if contact_client is not None else _default_contact_client()
            client_master = (
                client_master_client
                if client_master_client is not None
                else _default_client_master_client()
            )

            flow = handoff if handoff is not None else InquiryHandoff()
            with flow.session(dict(company=company, name=name, email=email, phone=phone)) as session:
                client_master_page_id = None
                if company:
                    matches = _exact_ids(client_master, _CLIENT_MASTER_TITLE_PROPERTY, company, 'title')
                    client_master_page_id = matches[0] if len(matches) == 1 else None
                saved = session.saved_page()
                if saved:
                    existing_page_id, current = saved, {}
                else:
                    try:
                        existing_page_id, current = _resolve_existing_contact(
                            contact, email=email, name=name, client_master_page_id=client_master_page_id
                        )
                    except AmbiguousContact as exc:
                        sync_result = session.ambiguous(str(exc))
                        return {'statusCode': 200, 'body': json.dumps({
                            'page_id': None, 'created': False,
                            'matched_client_master': client_master_page_id is not None, 'sync': sync_result})}

                if existing_page_id is not None:
                    with session.page_lock(existing_page_id):
                        invalid = session.check_page(contact, existing_page_id)
                        if invalid:
                            return {'statusCode': 200, 'body': json.dumps({
                                'page_id': existing_page_id, 'created': False,
                                'matched_client_master': client_master_page_id is not None, 'sync': invalid})}
                        current = contact.get_page(existing_page_id)
                        if current is None:
                            raise RuntimeError('既存連絡先の読戻しができません')
                        update_props: dict[str, Any] = {}
                        if email and not current.get(_EMAIL_PROPERTY):
                            update_props[_EMAIL_PROPERTY] = email
                        if phone and not current.get(_PHONE_PROPERTY):
                            update_props[_PHONE_PROPERTY] = phone
                        if client_master_page_id and not current.get(_CLIENT_MASTER_RELATION_PROPERTY):
                            update_props[_CLIENT_MASTER_RELATION_PROPERTY] = [client_master_page_id]
                        session.stage(existing_page_id, update_props)
                        if update_props:
                            contact.update_page(existing_page_id, update_props)
                    page_id = existing_page_id
                    created = False
                else:
                    create_props: dict[str, Any] = {_NAME_PROPERTY: name or email}
                    if email:
                        create_props[_EMAIL_PROPERTY] = email
                    if phone:
                        create_props[_PHONE_PROPERTY] = phone
                    if client_master_page_id:
                        create_props[_CLIENT_MASTER_RELATION_PROPERTY] = [client_master_page_id]
                    page_id = session.create(contact, create_props)
                    created = True
                sync_result = session.deliver(contact, page_id)
    except Exception:
        logger.exception("unexpected error while syncing lead-researcher inquiry to Notion contact db")
        return internal_error_response()

    if company and client_master_page_id is None:
        # 「companyが空だった」のか「companyはあったが一致しなかった」のかは
        # レスポンスJSONの`matched_client_master: false`だけでは区別できないため、
        # 後者のケースだけログにも残す（obasan-qualityレビューWARN対応）。
        logger.info(
            "lead_inquiry_webhook: no exact 取引先マスター match for company '%s' -- "
            "contact %s created/updated without a 取引先マスター link",
            company,
            page_id,
        )

    return {
        "statusCode": 200,
        "body": json.dumps(
            {
                "page_id": page_id,
                "created": created,
                "matched_client_master": client_master_page_id is not None,
                "sync": sync_result,
            }
        ),
    }
