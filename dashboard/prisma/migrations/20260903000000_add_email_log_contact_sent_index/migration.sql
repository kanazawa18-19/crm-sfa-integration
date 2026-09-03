-- 未返信リマインドの抽出（連絡先ごとの最新行）を、EmailLogが増えても速く保つ（2026-09-03）。
-- 追加のみ。既存の列・データには触らない。
--
-- src/email_reminders/db.py の find_latest_inbound_awaiting_reply() が毎時
--   SELECT DISTINCT ON ("contactPageId") … ORDER BY "contactPageId", "sentAt" DESC
-- を流している。"contactPageId" 単体のインデックスだけでは各連絡先ぶんのソートが要る。
-- scripts/backfill_gmail_history.py で過去1年分を取り込むと EmailLog が大きく増えるため、
-- **行が少ないうち（2026-09-03時点で309行）に張っておく。**
CREATE INDEX IF NOT EXISTS "EmailLog_contactPageId_sentAt_idx"
    ON "EmailLog" ("contactPageId", "sentAt");
