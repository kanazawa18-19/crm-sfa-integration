"""照合受付・版の比較・処理予約を同じトランザクションで保存する。"""
from __future__ import annotations

from contextlib import contextmanager
from email.utils import getaddresses

from psycopg.types.json import Jsonb
from src.slack_crm.storage import _connect
from src.email_review.domain import Conflict, Forbidden, decide, normalized
from src.email_review.body import current_body


def same_mail(left: dict, right: dict, *, allow_incomplete: bool = False) -> bool:
    """共通IDの衝突だけで別の受信者に本文へのアクセス権を渡さない。"""
    def addresses(message, key):
        return sorted(a.lower() for _, a in getaddresses([message.get(key) or ""]))
    if any(addresses(left, key) != addresses(right, key) for key in ("from_header", "to_header", "cc_header")):
        return False
    if (left.get("subject") or "") != (right.get("subject") or ""):
        return False
    # 取得前後の情報補完は許可するが、両方に本文がある場合は一致を必須とする。
    first, second = left.get("body_text"), right.get("body_text")
    if first and second:
        # 同じ所有者・同じGmail IDの再取得だけ、引用除去前後の移行を許可する。
        # 別所有者への共有では旧本文の引用を公開しないよう完全一致を維持する。
        if allow_incomplete:
            first, second = current_body(first), current_body(second)
            if not first.strip() or not second.strip():
                return False
        return first.replace("\r\n", "\n") == second.replace("\r\n", "\n")
    return allow_incomplete and (left.get("snippet") or "") == (right.get("snippet") or "")


class Repository:
    def actor(self, actor_id: str) -> str:
        with _connect() as conn:
            row = conn.execute('SELECT email FROM "User" WHERE id=%s', (actor_id,)).fetchone()
        if not row:
            raise Forbidden("ログイン情報を確認できません")
        return row["email"].lower()

    def slack_actor(self, email: str) -> str:
        with _connect() as conn:
            row = conn.execute('SELECT id FROM "User" WHERE lower(email)=%s', (email.lower(),)).fetchone()
        if not row:
            raise Forbidden("管理画面に登録された本人のSlackで操作してください")
        return row["id"]

    def capture(self, row: dict, owner: str, gmail_id: str, thread_id: str | None) -> None:
        with _connect() as conn:
            # 同じGmail IDの情報補完で受付を増やさない。共有メールの共通IDも直列化する。
            keys = ["email-owner:" + owner + ":" + gmail_id]
            if row.get("canonical_key"):
                keys.append("email-canonical:" + row["canonical_key"])
            for key in sorted(keys):
                conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', (key,))
            existing = conn.execute('''SELECT r.data FROM "EmailReview" r JOIN "EmailReviewOwner" o ON o."reviewId"=r.id
                WHERE o."ownerEmail"=%s AND o."gmailMessageId"=%s FOR UPDATE OF r''', (owner, gmail_id)).fetchone()
            canonical = conn.execute('SELECT data FROM "EmailReview" WHERE data->>\'canonical_key\'=%s FOR UPDATE',
                                     (row.get("canonical_key"),)).fetchone() if row.get("canonical_key") else None
            own_existing = bool(existing and canonical and existing["data"]["id"] == canonical["data"]["id"])
            if canonical and not same_mail(canonical["data"]["message"], row["message"], allow_incomplete=own_existing):
                raise Conflict("共通IDが同じ別内容のメールです。管理者による照合が必要です")
            if existing:
                old = existing["data"]
                if canonical and canonical["data"]["id"] != old["id"]:
                    # 共通ID未取得の行は作成不可。共通ID取得後は既存の受付へ寄せる。
                    if old.get("action_id") or old.get("canonical_key"):
                        raise Conflict("同じメールの受付が競合しています")
                    conn.execute('UPDATE "EmailReviewOwner" SET "reviewId"=%s WHERE "reviewId"=%s', (canonical["data"]["id"], old["id"]))
                    self.save(conn, old, {**old, "state": "excluded", "reason": "同じメールの受付に統合済み", "revision": old["revision"] + 1})
                    return
                # 同じ所有者・Gmail IDの実読に限り状態の証拠を補完する。完了行を再配送しない。
                from src.gmail_sync.mail_state import sent
                if sent(row["message"]) and not sent(old["message"]):
                    updated = {**old, "message": {**old["message"], "label_ids": row["message"]["label_ids"]},
                               "revision": old["revision"] + 1}
                    if old.get("reason", "").startswith("送信済みの証拠を確認できません") and not old.get("action_id") and not old.get("attempted_properties"):
                        updated.update(state="new", reason="送信済みを実読したため再照合します")
                    self.save(conn, old, updated)
                    old = updated
                if old["state"] in {"new", "pending", "held", "excluded"} and (
                    not old["message"].get("body_text") and row["message"].get("body_text")
                    or not old.get("canonical_key") and row.get("canonical_key")
                ):
                    updated = {**old, "message": row["message"], "canonical_key": row.get("canonical_key") or old.get("canonical_key"),
                               "revision": old["revision"] + 1}
                    if old["state"] in {"pending", "new"}:
                        updated["state"] = "new"
                    self.save(conn, old, updated)
                return
            if canonical:
                row = canonical["data"]
            inserted = conn.execute('INSERT INTO "EmailReview" (id,data) VALUES (%s,%s) ON CONFLICT DO NOTHING RETURNING id', (row["id"], Jsonb(row))).fetchone()
            conn.execute('''INSERT INTO "EmailReviewOwner" ("reviewId","ownerEmail","gmailMessageId","threadId")
                VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING''', (row["id"], owner, gmail_id, thread_id))
            if inserted and thread_id:
                waiting = conn.execute('''SELECT data FROM "EmailReview" WHERE data->>'state'='pending' AND id IN
                    (SELECT "reviewId" FROM "EmailReviewOwner" WHERE "ownerEmail"=%s AND "threadId"=%s)
                    ORDER BY id FOR UPDATE''', (owner, thread_id)).fetchall()
                for item in waiting:
                    old = item["data"]
                    self.save(conn, old, {**old, "state": "new", "revision": old["revision"] + 1,
                                         "reason": "同じ会話にメールが追加されたため再照合します"})

    def list(self, actor_id: str, *, state: str = "pending", offset: int = 0) -> list[dict]:
        owner = self.actor(actor_id)
        states = {"pending": ["new", "evaluating", "pending"], "held": ["held"], "excluded": ["excluded"], "approved": ["approved", "processing", "done", "unknown", "recheck", "reconciling"]}.get(state)
        if not states:
            raise ValueError("表示状態が不正です")
        with _connect() as conn:
            rows = conn.execute('''SELECT r.data, o."gmailMessageId" FROM "EmailReview" r
                JOIN "EmailReviewOwner" o ON o."reviewId"=r.id WHERE o."ownerEmail"=%s
                AND r.data->>'state'=ANY(%s) ORDER BY r."updatedAt" DESC,r.id LIMIT 30 OFFSET %s''', (owner, states, offset)).fetchall()
        return [{**self.visible(row["data"], owner), "gmail_id": row["gmailMessageId"], "owner": owner} for row in rows]

    def get(self, review_id: str, actor_id: str) -> dict:
        owner = self.actor(actor_id)
        with _connect() as conn:
            row = conn.execute('''SELECT r.data FROM "EmailReview" r WHERE r.id=%s AND EXISTS
                (SELECT 1 FROM "EmailReviewOwner" o WHERE o."reviewId"=r.id AND o."ownerEmail"=%s)''', (review_id, owner)).fetchone()
        if not row:
            raise Forbidden("本人の照合対象を開いてください")
        return self.visible(row["data"], owner)

    def visible(self, row: dict, owner: str) -> dict:
        evidence = row.get("evidence") or []
        ids = list({e.get("review_id") for e in evidence if e.get("review_id")})
        allowed = {row["id"]}
        if ids:
            with _connect() as conn:
                allowed.update(r["reviewId"] for r in conn.execute('''SELECT "reviewId" FROM "EmailReviewOwner"
                    WHERE "ownerEmail"=%s AND "reviewId"=ANY(%s)''', (owner, ids)).fetchall())
        # 共同受信者へ、別メールで集めた他人の署名や本文断片を公開しない。
        visible = lambda e: e.get("review_id") in allowed
        result = public(row)
        result["evidence"] = [e for e in evidence if visible(e)]
        result["candidates"] = [{**c, "evidence": [e for e in c.get("evidence", []) if visible(e)]} for c in row.get("candidates", [])]
        return result

    def thread(self, row: dict) -> list[dict]:
        if not row.get("thread_id"):
            return [row]
        with _connect() as conn:
            rows = conn.execute('''SELECT r.data FROM "EmailReview" r JOIN "EmailReviewOwner" o ON o."reviewId"=r.id
                WHERE o."ownerEmail"=%s AND o."threadId"=%s ORDER BY r."updatedAt" DESC LIMIT 100''', (row["owner"], row["thread_id"])).fetchall()
        return [item["data"] for item in rows]

    def masters(self, values: list[str] | None = None, query: str = "") -> list[dict]:
        with _connect() as conn:
            if values is not None:
                from src.migration.zoho_client_master import normalize_company_name_strong
                keys = list({normalize_company_name_strong(value).lower() for value in values})
                if not keys:
                    return []
                # 既存索引で候補だけ取得し、弱い正規化の完全一致で最終判定する。
                rows = conn.execute('SELECT "notionPageId" AS id,"rawName" AS name FROM "ClientNameIndex" WHERE lower("normalizedName")=ANY(%s)', (keys,)).fetchall()
                names = {normalized(value) for value in values}
                return [row for row in rows if normalized(row["name"]) in names]
            if not 2 <= len(query.strip()) <= 80:
                return []
            escaped = query.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            return conn.execute('''SELECT "notionPageId" AS id,"rawName" AS name FROM "ClientNameIndex"
                WHERE "rawName" ILIKE %s ESCAPE '\\' ORDER BY "rawName",id LIMIT 20''', (f"%{escaped}%",)).fetchall()

    @contextmanager
    def locked(self, review_id: str):
        with _connect() as conn:
            row = conn.execute('SELECT data FROM "EmailReview" WHERE id=%s FOR UPDATE', (review_id,)).fetchone()
            if not row:
                raise Conflict("受付がありません")
            yield conn, row["data"]

    def save(self, conn, old: dict, new: dict) -> None:
        conn.execute('UPDATE "EmailReview" SET data=%s,"updatedAt"=now() WHERE id=%s', (Jsonb(new), old["id"]))
        # 本文を判断履歴に複製しない。根拠・選択・操作主体・時刻を残す。
        conn.execute('INSERT INTO "EmailReviewHistory" ("reviewId",data) VALUES (%s,%s)', (old["id"], Jsonb({"before": public(old), "after": public(new)})))

    def decision(self, review_id: str, actor_id: str, revision: int, action: str, selection: dict, channel: str) -> dict:
        self.get(review_id, actor_id)
        with self.locked(review_id) as (conn, row):
            updated = decide(row, revision=revision, action=action, selection=selection, actor=actor_id, channel=channel)
            self.save(conn, row, updated)
        return self.visible(updated, self.actor(actor_id))

    def claim(self, state: str, next_state: str) -> dict | None:
        with _connect() as conn:
            row = conn.execute('''SELECT data FROM "EmailReview" WHERE data->>'state'=%s
                ORDER BY "updatedAt",id FOR UPDATE SKIP LOCKED LIMIT 1''', (state,)).fetchone()
            if not row:
                return None
            old = row["data"]
            updated = {**old, "state": next_state, "revision": old["revision"] + 1}
            self.save(conn, old, updated)
            return updated

    def finish(self, row: dict, **changes) -> dict:
        with self.locked(row["id"]) as (conn, current):
            if current["revision"] != row["revision"]:
                raise Conflict("処理中に状態が変わりました")
            updated = {**current, **changes, "revision": current["revision"] + 1}
            self.save(conn, current, updated)
        return updated

    def checkpoint(self, row: dict, **changes) -> dict:
        return self.finish(row, **changes)

    def recover_stale(self) -> None:
        with _connect() as conn:
            # 作成中断は時間切れを理由に再POSTしない。結果照合待ちに隔離する。
            rows = conn.execute('''SELECT data FROM "EmailReview"
                WHERE data->>'state' IN ('evaluating','processing','reconciling')
                AND "updatedAt" < now()-interval '20 minutes' ORDER BY id FOR UPDATE SKIP LOCKED''').fetchall()
            for item in rows:
                old = item["data"]
                state = "new" if old["state"] == "evaluating" else "unknown"
                if old["state"] == "processing" and not old.get("attempted_properties") and not old.get("action_id"):
                    state = "approved"
                self.save(conn, old, {**old, "state": state, "revision": old["revision"] + 1,
                                     "reason": "処理が中断しました。保存済みの段階から確認します"})


def action_actor(page_id: str) -> str | None:
    """作成済みの受付だけから、検証済み実施者を共通登録処理へ渡す。"""
    with _connect() as conn:
        if conn.execute('SELECT to_regclass(%s) AS name', ('"EmailReview"',)).fetchone()["name"] is None:
            return None
        rows = conn.execute('SELECT data FROM "EmailReview" WHERE data->>\'action_id\'=%s', (page_id,)).fetchall()
    if len(rows) != 1:
        return None
    return rows[0]["data"].get("actor_notion_id")


def public(row: dict) -> dict:
    from src.gmail_sync.mail_state import sent
    return {**{key: value for key, value in row.items() if key not in {"message", "body_text", "thread_id", "owner", "attempted_properties", "written_properties", "contact_completion"}},
            "sent_evidence": sent(row.get("message") or {})}


def action_registration_blocked(page_id: str) -> bool:
    """メール活動は受付の送信証拠がある場合だけ通常配送を許す。"""
    from src.gmail_sync.mail_state import sent
    from uuid import UUID
    variants = {page_id, page_id.replace("-", "")}
    try:
        identifier = UUID(page_id)
        variants.update((str(identifier), identifier.hex))
    except ValueError:
        pass
    with _connect() as conn:
        if conn.execute('SELECT to_regclass(%s) AS name', ('"EmailReview"',)).fetchone()["name"] is None:
            return False
        rows = conn.execute('''SELECT data FROM "EmailReview" WHERE data->>'action_id'=ANY(%s)''',
                            (list(variants),)).fetchall()
    if not rows:
        return False
    return len(rows) != 1 or rows[0]['data'].get('direction') != 'outbound' or not sent(rows[0]['data'].get('message') or {})
