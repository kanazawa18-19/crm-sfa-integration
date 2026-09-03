-- Gmailのスレッドidを保存する（2026-09-03）。追加のみ・NULL許容。
--
-- 返信ラグを「同じスレッド内の送信→受信」に限るために使う。無いと、別件で届いた
-- メールを直前の送信への返信として数えてしまい、複数案件を並行している相手ほど
-- 返信ラグが実態より短く出る（ChatGPTレビュー指摘）。
-- 既存行はNULLのまま。NULL同士は従来どおり時系列だけで判定する。
ALTER TABLE "EmailLog" ADD COLUMN IF NOT EXISTS "gmailThreadId" TEXT;
