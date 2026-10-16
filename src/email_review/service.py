"""メール取得と照合・登録・配送を分け、同じ受付を再開する。"""
from __future__ import annotations

import os
import logging
from dataclasses import asdict
from datetime import datetime, timezone

from src.email_review import domain
from src.email_review.repository import Repository, public
from src.email_review.gateway import Gateway
from src.sync_engine.record_sync_lock import RecordSyncBusy

logger = logging.getLogger(__name__)


def enabled() -> bool:
    return os.environ.get("EMAIL_REVIEW_ENABLED", "").lower() == "true"


def internal_domains() -> set[str]:
    """既存の取得設定を保持し、照合だけの追加社内ドメインを除外する。"""
    from src.gmail_sync.sync import internal_domains_from_env
    extra = os.environ.get("EMAIL_REVIEW_INTERNAL_DOMAINS", "")
    return set(internal_domains_from_env()) | {value.strip().lower() for value in extra.split(",") if value.strip()}


def safe_capture(message, owner: str) -> bool:
    try:
        capture(message, owner)
        return True
    except Exception as exc:
        logger.warning("メール照合受付の保存失敗: id=%s type=%s", message.id, type(exc).__name__)
        return False


def capture(message, owner: str, *, repo=None) -> None:
    if not enabled():
        return
    # 開始時刻が無ければ既存メールの大量登録を防いで設定エラーとする。
    start = datetime.fromisoformat(os.environ["EMAIL_REVIEW_START_AT"].replace("Z", "+00:00"))
    if start.tzinfo is None:
        raise ValueError("EMAIL_REVIEW_START_ATにはタイムゾーンを指定してください")
    data = asdict(message)
    if domain.authentication_notice(data):
        return
    stamp = data.get("internal_date_ms")
    if not stamp or datetime.fromtimestamp(int(stamp) / 1000, timezone.utc) < start:
        return
    owner = owner.lower()
    direction, people = domain.participants(data, owner, internal_domains())
    if not people:
        return
    review_id = domain.identity(data, owner)
    row = {"id": review_id, "owner": owner, "thread_id": data.get("thread_id"), "message": data,
           "activity_owner": owner,
           "canonical_key": review_id if data.get("message_id_header") else None,
           "subject": data.get("subject") or "件名なし", "participants": people, "direction": direction,
           "state": "new" if direction == "outbound" else "excluded",
           "delivery": "not_started", "revision": 0,
           "reason": "照合処理待ち" if direction == "outbound" else domain.OUTBOUND_ONLY_REASON,
           "candidates": [], "contacts": [], "evidence": []}
    (repo or Repository()).capture(row, owner, message.id, message.thread_id)


class Service:
    def __init__(self, repo=None, gateway=None):
        self.repo = repo or Repository()
        self.gateway = gateway or Gateway()

    def evaluate(self, row: dict) -> dict:
        if row.get("direction") != "outbound":
            return self.repo.finish(row, state="excluded", reason=domain.OUTBOUND_ONLY_REASON)
        people = row["participants"]
        contacts = self.gateway.contacts([p["email"] for p in people])
        evidence_rows = []
        if len(people) == 1:
            for item in self.repo.thread(row):
                if len(item["participants"]) == 1 and item["participants"][0]["email"] == people[0]["email"]:
                    evidence_rows += [{**e, "review_id": item["id"]} for e in domain.evidence(item["message"], item["direction"], people[0]["email"])]
        masters = self.repo.masters([item["value"] for item in evidence_rows])
        affiliations = contacts[0]["affiliations"] if len(contacts) == 1 else []
        result = domain.match(evidence_rows, masters, affiliations)
        result.update(contacts=contacts, evidence=evidence_rows)
        if not row.get("canonical_key"):
            result.update(state="pending", reason="メール共通IDがありません。本文取得の設定後に再取得してください")
        if len(people) != 1 or len(contacts) != 1:
            result.update(state="pending", reason="相手の連絡先を確認してください（未登録または複数候補）")
        if result["state"] == "approved":
            selection = self.gateway.validate(row, {"company_id": result["company_id"], "contact_id": contacts[0]["id"]})
            result.update(selection, actor="system", channel="automatic", delivery="queued")
        return self.repo.finish(row, **result)

    def decision(self, review_id: str, actor_id: str, revision: int, action: str, selection: dict, channel="dashboard") -> dict:
        row = self.repo.get(review_id, actor_id)
        # 重い外部照合より前にも版と状態を検査。保存時にもう一度原子的に検査する。
        domain.decide(row, revision=revision, action=action, selection=selection, actor=actor_id, channel=channel)
        if action == "confirm":
            if not row.get("canonical_key"):
                raise domain.Conflict("メール共通IDの取得が必要です")
            from uuid import UUID
            selection = {key: str(UUID(value)) if value else None for key, value in selection.items()}
            if selection["contact_id"] not in {c["id"] for c in row.get("contacts", [])}:
                raise domain.Conflict("連絡先候補を再照合してください")
        # 外部APIの実物照合は処理予約後に行う。Slackの受付応答を待たせない。
        return self.repo.decision(review_id, actor_id, revision, action, selection, channel)

    def process(self, row: dict) -> dict:
        if row.get("direction") != "outbound":
            # 旧版の予約も外部書込み前に止める。作成結果不明の追跡は残す。
            state = "unknown" if row.get("attempted_properties") and not row.get("action_id") else "held" if row.get("action_id") else "excluded"
            return self.repo.finish(row, state=state, reason=domain.OUTBOUND_ONLY_REASON)
        try:
            if row.get("attempted_properties") and not row.get("action_id"):
                return self.repo.finish(row, state="unknown", reason="作成試行済みです。既存登録を照合してください")
            validated = self.gateway.validate(row, row)
            actor_id = self.gateway.actor(row.get("activity_owner") or row["owner"])
            row = self.repo.checkpoint(row, **{k: validated[k] for k in ("company_name", "contact_name", "target_name", "project_name")}, actor_notion_id=actor_id)
            props = self.gateway.action_properties(row)
            contact_changes, owned_link = self.gateway.link_contact(row)
            row = self.repo.checkpoint(row, linked_company_id=row["company_id"] if owned_link else None,
                                       linked_contact_id=row["contact_id"] if owned_link else None)
            if not row.get("action_id"):
                # claim(processing) が作成予約。POST後に停止しても再作成しない。
                row = self.repo.checkpoint(row, attempted_properties=props)
                page_id = self.gateway.create_action(props)
                row = self.repo.checkpoint(row, action_id=page_id, written_properties=props)
            else:
                self.gateway.update_action(row, props)
                row = self.repo.checkpoint(row, written_properties=props)
            contact_state = self.gateway.dispatch("contact", row["contact_id"], contact_changes)
            action_state = self.gateway.dispatch("action", row["action_id"], props)
            return self.repo.finish(row, state="done", delivery="delivered" if contact_state == action_state == "delivered" else "partial",
                                    reason="記録作成済み", delivery_details={"contact": contact_state, "action": action_state})
        except domain.Conflict as exc:
            return self.repo.finish(row, state="pending", reason=str(exc), delivery="partial" if row.get("action_id") else "not_started")
        except RecordSyncBusy:
            return self.repo.finish(row, state="approved", reason="同じ対象の処理が終わってから再開します")
        except Exception as exc:
            # 例外本文はメール・認証情報を含み得るので保存しない。
            logger.warning("メール照合の外部処理失敗: id=%s type=%s", row["id"], type(exc).__name__)
            if row.get("action_id"):
                return self.repo.finish(row, state="done", delivery="partial", reason="記録作成済み・配送未完了。関連先を確認して再受付できます")
            unknown = bool(row.get("attempted_properties"))
            return self.repo.finish(row, state="unknown" if unknown else "pending", delivery="unknown" if unknown else "not_started",
                                    reason="登録結果を確認してください。再登録は行いません" if unknown else "処理に失敗しました。関連先を確認して再受付してください")

    def reconcile(self, row: dict) -> dict:
        try:
            page_id = row.get("action_id") or self.gateway.find_action(row)
            if page_id:
                return self.repo.finish(row, action_id=page_id, written_properties=row.get("attempted_properties") or row.get("written_properties"),
                                        state="approved", reason="既存の登録を確認。同じ記録の配送を再開します")
        except Exception:
            pass
        return self.repo.finish(row, state="unknown", reason="既存登録を一意に確認できません。管理者による原本照合が必要です。再登録は行いません")

    def drain(self, limit=5) -> dict:
        if not enabled():
            return {"enabled": False, "evaluated": 0, "processed": 0}
        self.repo.recover_stale()
        evaluated = processed = 0
        for _ in range(limit):
            row = self.repo.claim("recheck", "reconciling")
            if not row:
                break
            self.reconcile(row)
        for _ in range(limit):
            row = self.repo.claim("new", "evaluating")
            if not row:
                break
            try:
                self.evaluate(row)
            except Exception:
                self.repo.finish(row, state="pending", reason="候補の読込みに失敗しました。再照合してください")
            evaluated += 1
        for _ in range(limit):
            row = self.repo.claim("approved", "processing")
            if not row:
                break
            try:
                self.process(row)
            except domain.Conflict:
                logger.warning("メール照合の処理権が更新されました: id=%s", row["id"])
            processed += 1
        return {"enabled": True, "evaluated": evaluated, "processed": processed}
