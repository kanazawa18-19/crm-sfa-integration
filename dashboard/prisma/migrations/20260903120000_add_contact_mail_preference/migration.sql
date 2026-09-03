-- 連絡先ごとの配信停止フラグ（2026-09-03、一斉配信）。追加のみ。
--
-- 特定電子メール法の「停止の申し出を受けたら以後送らない」を満たすための記録。
-- 書き込むのは公開ページ（/unsubscribe）だけ、バックエンドは読み取り専用。
-- contactPageId はハイフン無し・小文字に正規化した形で入る。
CREATE TABLE IF NOT EXISTS "ContactMailPreference" (
    "id" TEXT NOT NULL,
    "contactPageId" TEXT NOT NULL,
    "contactEmail" TEXT NOT NULL,
    "unsubscribed" BOOLEAN NOT NULL DEFAULT true,
    "unsubscribedAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "source" TEXT NOT NULL DEFAULT 'self',
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updatedAt" TIMESTAMP(3) NOT NULL,

    CONSTRAINT "ContactMailPreference_pkey" PRIMARY KEY ("id")
);

CREATE UNIQUE INDEX IF NOT EXISTS "ContactMailPreference_contactPageId_key"
    ON "ContactMailPreference"("contactPageId");

CREATE INDEX IF NOT EXISTS "ContactMailPreference_contactEmail_idx"
    ON "ContactMailPreference"("contactEmail");

-- contactPageId は「正規化済み（ハイフン無し・小文字の32桁hex）」という約束だが、
-- Prismaの型は TEXT でしかなく、約束はコメントにしか無い状態だった。
-- 将来 source='manual'（社内で手入力）の書き込みが増えたときに
-- `ABC-DEF…` と `abcdef…` が別レコードとして入り、片方だけ停止扱いになる余地がある。
-- 形そのものをDBで固定しておく（ChatGPTレビュー指摘、2026-09-03）。
ALTER TABLE "ContactMailPreference"
    DROP CONSTRAINT IF EXISTS "ContactMailPreference_contactPageId_normalized";
ALTER TABLE "ContactMailPreference"
    ADD CONSTRAINT "ContactMailPreference_contactPageId_normalized"
    CHECK ("contactPageId" ~ '^[0-9a-f]{32}$');

