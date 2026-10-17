"""既存のNotion正本・共通配送処理への接続。"""
from __future__ import annotations

from contextlib import contextmanager, ExitStack
from datetime import datetime, timezone, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from src.db_schema.registry import get_schema
from src.db_schema.base import Tool
from src.sync_engine.clients.notion_client import HttpNotionClient
from src.sync_engine.webhook_handlers.notion_webhook import parse_notion_property_value
from src.sync_engine.record_sync_lock import _connect_direct, lock_key, RecordSyncBusy
from src.sync_engine.sync_event import SyncEvent
from src.email_review.domain import Conflict, normalized, same_value, require_outbound, match_existing_contact
from src.email_review.body import current_body


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
        memo = (raw.get("properties", {}).get("備考") or {}).get("rich_text", [])
        plain = all(v.get("type") == "text" and not v.get("href") and not (v.get("text") or {}).get("link")
                    and not any(value for k, value in (v.get("annotations") or {}).items() if k != "color")
                    and (v.get("annotations") or {}).get("color", "default") == "default" for v in memo)
        result = {"_last_edited_time": raw.get("last_edited_time"), "_memo_plain": plain}
        for name, value in (raw.get("properties") or {}).items():
            if value.get("type") in {"title", "rich_text", "relation", "email", "select", "date", "phone_number"}:
                result[name] = parse_notion_property_value(value)
        return result

    def contacts(self, addresses: list[str]) -> list[dict]:
        result = []
        client = self.client("contact")
        for address in sorted({value.strip().casefold() for value in addresses}):
            pages = client.query_all_pages(filter={"property": "メールアドレス", "email": {"equals": address}})
            for page in pages:
                if page.get("archived") or page.get("in_trash"):
                    continue
                props = page.get("properties") or {}
                email = parse_notion_property_value(props["メールアドレス"])
                if not isinstance(email, str) or email.strip().casefold() != address:
                    continue
                current = self.live("contact", page["id"])
                if str(current.get("メールアドレス") or "").strip().casefold() != address:
                    continue
                result.append({"id": page["id"], "name": current.get("名前") or address,
                               "email": address, "affiliations": current.get("取引先マスター") or []})
        return list({item["id"]: item for item in result}.values())

    def company_contacts(self, company_id: str) -> list[dict]:
        pages = self.client("contact").query_all_pages(filter={"property": "取引先マスター", "relation": {"contains": company_id}})
        # 一意性の候補一覧は検索結果で数え、採用候補だけをliveで再取得する。
        return [{"id": p["id"], **{k: parse_notion_property_value(v) for k, v in p.get("properties", {}).items()
                                  if k in {"名前", "姓", "名", "メールアドレス"}}}
                for p in pages if not p.get("archived") and not p.get("in_trash")]

    def plan_completion(self, row: dict, companies: list[dict], names: list[str], email: str) -> dict | None:
        from src.email_review.contact_completion import contact_changes, unique_named_contact
        companies = {c["id"]: c for c in companies}
        if len(companies) != 1:
            return None
        company_id, company = next(iter(companies.items()))
        if normalized(self.live("client_master", company_id).get("取引先名") or "") != normalized(company["name"]):
            return None
        matches = self.contacts([email])
        if len(matches) > 1:
            return None
        replace = not matches
        if matches:
            contact = {"id": matches[0]["id"], **self.live("contact", matches[0]["id"])}
            if contact.get("取引先マスター"):
                return None
        else:
            contact = unique_named_contact(self.company_contacts(company_id), names)
            if not contact:
                return None
            contact = {"id": contact["id"], **self.live("contact", contact["id"])}
        try:
            if replace:
                self.require_recent_email(row, contact)
            changes = contact_changes(contact, email=email, company_id=company_id, replace_email=replace)
        except (ValueError, Conflict) as exc:
            return {"state": "pending", "reason": str(exc)}
        expected = {k: contact.get(k) for k in set(changes) | {"名前", "メールアドレス", "取引先マスター", "姓", "名", "備考"}}
        return {"state": "approved", "reason": "メール・取引先・氏名から既存連絡先を補完", "match_basis": "contact_completion",
                "contact_id": contact["id"], "company_id": company_id, "target_id": company_id, "project_id": None,
                "contact_completion": {"expected": expected, "changes": changes, "email": email,
                    "company_name": company["name"], "names": names, "replace_email": replace}}

    @staticmethod
    def require_recent_email(row: dict, contact: dict) -> None:
        try:
            sent = datetime.fromtimestamp(int(row["message"]["internal_date_ms"]) / 1000, timezone.utc)
            edited = datetime.fromisoformat(contact["_last_edited_time"].replace("Z", "+00:00"))
            if sent >= edited + timedelta(minutes=1):
                return
        except (KeyError, TypeError, ValueError, OverflowError, AttributeError):
            pass
        raise Conflict("連絡先更新前のメールによるアドレス差替えは保留します")

    def validate_completion(self, row: dict, selection: dict) -> None:
        from src.email_review.contact_completion import unique_named_contact
        plan = row["contact_completion"]
        if any(selection.get(k) != row.get(k) for k in ("company_id", "contact_id", "target_id", "project_id")):
            raise Conflict("補完計画の対象が変わっています")
        if normalized(self.live("client_master", row["company_id"]).get("取引先名") or "") != normalized(plan["company_name"]):
            raise Conflict("補完対象の会社名が変わっています")
        current = self.live("contact", row["contact_id"])
        if "備考" in plan["changes"] and not current.get("_memo_plain", True):
            raise Conflict("備考に書式・リンクがあるため、メール変更は手動確認してください")
        before, after = plan["expected"], {**plan["expected"], **plan["changes"]}
        def equal(expected):
            return all((current.get(k) or None) == (v or None) for k, v in expected.items())
        if plan["replace_email"] and not equal(after):
            self.require_recent_email(row, current)
        if not equal(before) and not equal(after):
            raise Conflict("連絡先が更新されています。再照合してください")
        matches = self.contacts([plan["email"]])
        if matches and {c["id"] for c in matches} != {row["contact_id"]}:
            raise Conflict("メールアドレスが別の連絡先に登録されています")
        if not plan["replace_email"] and len(matches) != 1:
            raise Conflict("メールアドレスの一致を確認できません")
        if plan["replace_email"]:
            winner = unique_named_contact(self.company_contacts(row["company_id"]), plan["names"])
            if not winner or winner["id"] != row["contact_id"]:
                raise Conflict("同じ取引先の氏名候補が変わっています")

    def validate(self, row: dict, selection: dict) -> dict:
        company_id = selection["company_id"]
        company = self.live("client_master", company_id)
        completion = row.get("match_basis") == "contact_completion" and bool(row.get("contact_completion")) and not row.get("contact_completion_applied")
        email_match = row.get("channel") == "automatic" and row.get("match_basis") == "contact_email"
        if completion:
            self.validate_completion(row, selection)
        elif email_match:
            # 評価後にメール・所属・同一メールの件数が変わっていないか、作成直前にも確認する。
            current = match_existing_contact(row["message"], row["participants"],
                self.contacts([p["email"] for p in row["participants"]]))
            if current["state"] != "approved" or any(current[k] != selection.get(k) for k in ("company_id", "contact_id", "target_id", "project_id")):
                raise Conflict("メールアドレスまたは連絡先の所属が変わっています。再照合してください")
        elif row.get("channel") == "automatic":
            chosen = next((c for c in row.get("candidates", []) if c["id"] == company_id), None)
            if not chosen or normalized(chosen["name"]) != normalized(company.get("取引先名") or ""):
                raise Conflict("会社名が変わっています。再照合してください")
        contact = self.live("contact", selection["contact_id"])
        addresses = {p["email"].strip().casefold() for p in row["participants"]}
        if not completion and str(contact.get("メールアドレス") or "").strip().casefold() not in addresses:
            raise Conflict("メールの相手と連絡先が一致しません")
        # 手動確定でも別会社への所属付け替えを暗黙に行わない。
        affiliations = contact.get("取引先マスター") or []
        if email_match and set(affiliations) != {company_id}:
            raise Conflict("連絡先の所属が変わっています。再照合してください")
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
        with ExitStack() as locks:
            if row.get("contact_completion"):
                locks.enter_context(record_lock("contact", "email:" + row["contact_completion"]["email"].strip().casefold()))
            locks.enter_context(record_lock("contact", row["contact_id"]))
            self.validate(row, row)
            current = self.live("contact", row["contact_id"])
            changed = set(current.get("取引先マスター") or []) != {row["company_id"]}
            from src.email_review.contact_completion import name_parts
            changes = name_parts(current)
            if changed:
                changes["取引先マスター"] = [row["company_id"]]
            if row.get("match_basis") == "contact_completion" and row.get("contact_completion"):
                if row.get("contact_completion_applied"):
                    changes.update({k: current.get(k) for k in row["contact_completion"]["changes"]})
                else:
                    changes.update(row["contact_completion"]["changes"])
            patch = {k: v for k, v in changes.items() if (current.get(k) or None) != (v or None)}
            if patch:
                self.client("contact").update_page(row["contact_id"], patch)
                saved = self.live("contact", row["contact_id"])
                if any((saved.get(k) or None) != (v or None) for k, v in changes.items()):
                    raise Conflict("連絡先補完の保存値を確認できません")
        owned = changed or (row.get("linked_contact_id") == row["contact_id"] and row.get("linked_company_id") == row["company_id"])
        return {"取引先マスター": [row["company_id"]], **changes}, owned

    def action_properties(self, row: dict) -> dict:
        require_outbound(row)
        message = row["message"]
        body = current_body(message.get("body_text") or "").replace("\r\n", "\n")
        if not isinstance(body, str) or not body.strip():
            raise Conflict("メール本文の全文を取得できないため、記録を保留します。元メールを確認してください")
        if len(body) > 20000:
            raise Conflict("引用を除いた本文が2万文字を超えるため、記録を保留します")
        # Notionの保存精度（分単位・ミリ秒表記）に揃える。元の送信時刻は受付に保持する。
        day = datetime.fromtimestamp(int(message["internal_date_ms"]) / 1000, tz=ZoneInfo("Asia/Tokyo")).replace(second=0, microsecond=0).isoformat(timespec="milliseconds")
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
