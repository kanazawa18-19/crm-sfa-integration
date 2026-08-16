"""lead-researcher（別リポジトリ、問い合わせメール自動調査Slackボット）からのWebhook受信。

lead-researcherがメールから抽出したリード情報（会社名/名前/メール/電話）を、Notion連絡先DBへ
find-or-createで反映する。web_engagement_webhook.pyと同じ設計方針を踏襲する:
`Dispatcher`/`IdMappingStore`は経由せず、Any-to-Any同期の汎用機構の外側で完結させる
（新規レコード作成はDispatcherのスコープ外のため）。

想定ペイロード:
{
  "company": "株式会社サンプル温泉",
  "name": "山田太郎",
  "email": "yamada@example.com",
  "phone": "090-1111-2222"
}
`email`と`name`はどちらか一方があればよい（両方空ならエラー）。

**連絡先の突合キー**: メールアドレス優先。無ければ名前（完全一致）でフォールバックするが、
同姓同名の別人を誤って同一人物とみなすリスクがあるため、`取引先マスター`が会社名の完全一致で
特定できている場合に限り、かつその取引先マスターが既存連絡先のリンク先と一致する場合のみ
フォールバックを採用する（2026-08-14、金沢さん指示「メアドと名前見て重複するのがあったら」
＋shirokuma-secレビューWARN対応）。それ以外（会社が特定できない、または既存連絡先の
リンク先と食い違う）は別人の可能性が高いとみなし、新規連絡先として作成する
（`logger.warning`で監査できるよう記録する）。

**取引先マスターへのリンクは会社名の完全一致時のみ**: あいまい一致・新規作成は行わない
（2026-08-14、金沢さん指示「ない場合に新規作成を無数にすることにならないように」）。
この連携専用の割り切りとして、`取引先マスター`（本来contact.pyではREQUIRED）を空のまま
作成することを許容する。

**既存連絡先への追記は「空欄なら埋める」方式で、上書きはしない**。対象プロパティ
（メールアドレス/携帯番号/取引先マスター）はいずれもsync_scope=ALL_TOOLSのため、
Notion側Webhook経由でDispatcherが無条件にkintone/Zohoへ伝播する。メール本文からの
LLM抽出は誤りうるが、既存の非空値を上書きすることはない設計であり、2026-08-14に
金沢さんへ確認の上でこのリスクを許容している（web_engagement_webhook.pyの
2026-08-13 BLOCKER対応とは異なり、ここでは意図的にALL_TOOLSプロパティへの書き込みを行う）。
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Protocol

from src.audit_log.actor_context import set_actor
from src.db_schema.registry import get_schema
from src.sync_engine.clients.notion_client import HttpNotionClient
from src.sync_engine.clients.notion_lookup import find_page_id_by_email, find_page_id_by_title
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
        page_id = find_page_id_by_email(contact, _EMAIL_PROPERTY, email)
        if page_id is not None:
            return page_id, contact.get_page(page_id) or {}

    if name and client_master_page_id:
        candidate_id = find_page_id_by_title(contact, _NAME_PROPERTY, name)
        if candidate_id is not None:
            candidate = contact.get_page(candidate_id) or {}
            candidate_client_masters = candidate.get(_CLIENT_MASTER_RELATION_PROPERTY) or []
            if client_master_page_id in candidate_client_masters:
                return candidate_id, candidate
            logger.warning(
                "lead_inquiry_webhook: name '%s' matched existing contact %s but its "
                "取引先マスター (%s) does not include the resolved company %s -- treating "
                "as a different person and creating a new contact instead of merging",
                name,
                candidate_id,
                candidate_client_masters,
                client_master_page_id,
            )

    return None, {}


def handler(
    event: Mapping[str, Any],
    context: object,
    *,
    contact_client: LeadInquiryContactClient | None = None,
    client_master_client: LeadInquiryContactClient | None = None,
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

            client_master_page_id = None
            if company:
                client_master_page_id = find_page_id_by_title(
                    client_master, _CLIENT_MASTER_TITLE_PROPERTY, company
                )

            existing_page_id, current = _resolve_existing_contact(
                contact, email=email, name=name, client_master_page_id=client_master_page_id
            )

            if existing_page_id is not None:
                update_props: dict[str, Any] = {}
                if email and not current.get(_EMAIL_PROPERTY):
                    update_props[_EMAIL_PROPERTY] = email
                if phone and not current.get(_PHONE_PROPERTY):
                    update_props[_PHONE_PROPERTY] = phone
                if client_master_page_id and not current.get(_CLIENT_MASTER_RELATION_PROPERTY):
                    update_props[_CLIENT_MASTER_RELATION_PROPERTY] = [client_master_page_id]
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
                page_id = contact.create_page(create_props)
                created = True
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
            }
        ),
    }
