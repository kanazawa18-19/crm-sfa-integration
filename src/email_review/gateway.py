"""既存のNotion正本・共通配送処理への接続。"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import UUID
from zoneinfo import ZoneInfo

from src.db_schema.registry import get_schema
from src.db_schema.base import Tool
from src.sync_engine.clients.notion_client import HttpNotionClient
from src.sync_engine.webhook_handlers.notion_webhook import parse_notion_property_value
from src.sync_engine.record_sync_lock import _connect_direct, lock_key, RecordSyncBusy
from src.sync_engine.sync_event import SyncEvent
from src.email_review.domain import Conflict, normalized, same_value, require_outbound


@contextmanager
def record_lock(db_key: str, key: str):
    conn = _connect_direct()
    try:
        conn.autocommit = True
        if not conn.execute('SELECT pg_try_advisory_lock(%s) AS locked', (lock_key(db_key, key),)).fetchone()["locked"]:
            raise RecordSyncBusy("同じ対象を処理中です")
        yield
    finally:
        conn.close()


class Gateway:
    def client(self, key: str):
        schema = get_schema(key)
        return HttpNotionClient(key, schema.notion_database_id)

    def live(self, key: str, page_id: str) -> dict:
        from src.record_merge.aliases import resolve_alias, require_record_available
        page_id = str(UUID(page_id))
        if resolve_alias(key, Tool.NOTION, page_id):
            raise Conflict("統合済みの候補です。正本を選び直してください")
        require_record_available(key, page_id)
        client = self.client(key)
        schema = get_schema(key)
        raw = client.get_raw_page_with_relations(page_id, {p.name for p in schema.properties if p.relation_target})
        if not raw or raw.get("archived") or raw.get("in_trash"):
            raise Conflict("候補が削除・保管されています")
        parent = raw.get("parent") or {}
        if str(parent.get("database_id", "")).replace("-", "") != schema.notion_database_id.replace("-", ""):
            raise Conflict("候補の種類が違います")
        result = {}
        for name, value in (raw.get("properties") or {}).items():
            if value.get("type") in {"title", "rich_text", "relation", "email", "select", "date", "phone_number"}:
                result[name] = parse_notion_property_value(value)
        return result

    def contacts(self, addresses: list[str]) -> list[dict]:
        result = []
        client = self.client("contact")
        for address in addresses:
            pages = client.query_all_pages(filter={"property": "メールアドレス", "email": {"equals": address}})
            for page in pages:
                if page.get("archived") or page.get("in_trash"):
                    continue
                props = page.get("properties") or {}
                email = parse_notion_property_value(props["メールアドレス"])
                if not isinstance(email, str) or email.casefold() != address.casefold():
                    continue
                current = self.live("contact", page["id"])
                result.append({"id": page["id"], "name": current.get("名前") or address,
                               "email": address, "affiliations": current.get("取引先マスター") or []})
        return list({item["id"]: item for item in result}.values())

    def validate(self, row: dict, selection: dict) -> dict:
        company_id = selection["company_id"]
        company = self.live("client_master", company_id)
        if row.get("channel") == "automatic":
            chosen = next((c for c in row.get("candidates", []) if c["id"] == company_id), None)
            if not chosen or normalized(chosen["name"]) != normalized(company.get("取引先名") or ""):
                raise Conflict("会社名が変わっています。再照合してください")
        contact = self.live("contact", selection["contact_id"])
        addresses = {p["email"] for p in row["participants"]}
        if str(contact.get("メールアドレス") or "").lower() not in addresses:
            raise Conflict("メールの相手と連絡先が一致しません")
        # 手動確定でも別会社への所属付け替えを暗黙に行わない。
        affiliations = contact.get("取引先マスター") or []
        owned_correction = (row.get("channel") in {"dashboard", "slack"}
                            and row.get("linked_contact_id") == selection["contact_id"]
                            and set(affiliations) == {row.get("linked_company_id")})
        if affiliations and set(affiliations) != {company_id} and not owned_correction:
            raise Conflict("連絡先の既存所属と異なります。所属を確認してください")
        target = selection.get("target_id") or company_id
        target_company = self.live("client_master", target)
        project_name = None
        if selection.get("project_id"):
            project = self.live("project", selection["project_id"])
            if set(project.get("取引先マスター") or []) != {target}:
                raise Conflict("案件と対象施設の組合せが変わっています")
            project_name = project.get("案件名") or "名称なし"
        return {**selection, "company_name": company.get("取引先名") or "名称なし", "contact_name": contact.get("名前") or "名前未設定",
                "target_name": target_company.get("取引先名") or "名称なし", "project_name": project_name}

    def actor(self, owner: str) -> str:
        from src.sync_engine.owner_mapping import owner_directory
        directory = owner_directory()
        matches = [r for r in (directory or {}).get("users", []) if str(r.get("email") or "").lower() == owner.lower()]
        if len(matches) != 1:
            raise Conflict("メール担当者とCRMユーザーの確認済み対応を設定してください")
        user_id = str(UUID(matches[0]["notion_id"]))
        user = self.client("action").get_user(user_id)
        if user.get("type") != "person" or str((user.get("person") or {}).get("email") or "").lower() != owner.lower():
            raise Conflict("メール担当者とNotionユーザーが一致しません")
        return user_id

    def link_contact(self, row: dict) -> tuple[dict, bool]:
        with record_lock("contact", row["contact_id"]):
            self.validate(row, row)
            current = self.live("contact", row["contact_id"])
            changed = set(current.get("取引先マスター") or []) != {row["company_id"]}
            if changed:
                self.client("contact").update_page(row["contact_id"], {"取引先マスター": [row["company_id"]]})
        owned = changed or (row.get("linked_contact_id") == row["contact_id"] and row.get("linked_company_id") == row["company_id"])
        return {"取引先マスター": [row["company_id"]]}, owned

    def action_properties(self, row: dict) -> dict:
        require_outbound(row)
        message = row["message"]
        body = message.get("body_text")
        if not isinstance(body, str) or not body.strip():
            raise Conflict("メール本文の全文を取得できないため、記録を保留します。元メールを確認してください")
        day = datetime.fromtimestamp(int(message["internal_date_ms"]) / 1000, tz=ZoneInfo("Asia/Tokyo")).isoformat()
        title = f"【メール】{message.get('subject') or '件名なし'} #mail-{row['id'][:12]}"
        return {"商談回数・電話回数・メール回数（何回目）": title[:180],
                "アクション種別": "メール",
                "アクション日": day, "先方担当者": row["contact_name"],
                "👨‍👩‍👧‍👦 取引先マスター": [row.get("target_id") or row["company_id"]],
                "案件名": [row["project_id"]] if row.get("project_id") else [],
                "履歴メモ": body}

    def create_action(self, properties: dict) -> str:
        return self.client("action").create_page_once(properties)

    def find_action(self, row: dict) -> str | None:
        expected = row.get("attempted_properties") or {}
        title_key = "商談回数・電話回数・メール回数（何回目）"
        if not expected.get(title_key):
            return None
        pages = self.client("action").query_all_pages(filter={"property": title_key, "title": {"equals": expected[title_key]}})
        ids = [p["id"] for p in pages if not p.get("archived") and not p.get("in_trash")]
        if len(ids) != 1:
            return None
        current = self.live("action", ids[0])
        if any(not same_value(key, current.get(key), value) for key, value in expected.items()):
            return None
        return ids[0]

    def update_action(self, row: dict, properties: dict) -> None:
        with record_lock("action", row["action_id"]):
            current = self.live("action", row["action_id"])
            previous = row.get("written_properties") or {}
            if any(not same_value(key, current.get(key), previous.get(key)) and not same_value(key, current.get(key), value) for key, value in properties.items()):
                raise Conflict("記録が外部で編集されています。内容を比較してください")
            self.client("action").update_page(row["action_id"], properties)

    def dispatch(self, key: str, page_id: str, properties: dict) -> str:
        from src.sync_engine.production_wiring import get_production_wiring
        raw = self.client(key).get_raw_page(page_id)
        occurred = datetime.fromisoformat(raw["last_edited_time"].replace("Z", "+00:00"))
        event = SyncEvent(source_tool=Tool.NOTION, db_key=key, external_id=page_id, occurred_at=occurred,
                          properties=properties, expected_source_properties=properties)
        result = get_production_wiring().dispatcher.dispatch(event)
        return "partial" if result.skipped or result.has_partial_skips else "delivered"
