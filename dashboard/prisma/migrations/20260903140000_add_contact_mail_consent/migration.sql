-- 連絡先ごとの「送ってよい根拠」（2026-09-03、一斉配信）。追加のみ。
--
-- ContactMailPreference（＝送ってはいけない人の名簿）だけでは、
-- 「名簿に載っていない＝送ってよい」という扱いになってしまう。
-- 特定電子メール法は広告宣伝メールを原則として同意を得た相手に送ることを求めるため、
-- 送ってよい理由を1件ずつ持ち、行が無い連絡先には送らない（既定で送信不可）。
--
-- 書き込むのは社内の管理画面だけ。バックエンド（src/bulk_email/db.py）は読み取り専用。
CREATE TABLE IF NOT EXISTS "ContactMailConsent" (
    "id" TEXT NOT NULL,
    "contactPageId" TEXT NOT NULL,
    "contactEmail" TEXT NOT NULL,
    "basis" TEXT NOT NULL,
    -- 取得日は時刻ではなく暦の日。TIMESTAMP で持つと、日本時間の午前中に当日を
    -- 登録したものが UTC 基準で「未来日」になり、送信不可になってしまう。
    "obtainedAt" DATE NOT NULL,
    "evidence" TEXT NOT NULL,
    "recordedBy" TEXT NOT NULL,
    "revokedAt" TIMESTAMP(3),
    "revokedBy" TEXT,
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updatedAt" TIMESTAMP(3) NOT NULL,

    CONSTRAINT "ContactMailConsent_pkey" PRIMARY KEY ("id")
);

CREATE UNIQUE INDEX IF NOT EXISTS "ContactMailConsent_contactPageId_key"
    ON "ContactMailConsent"("contactPageId");

CREATE INDEX IF NOT EXISTS "ContactMailConsent_contactEmail_idx"
    ON "ContactMailConsent"("contactEmail");

CREATE INDEX IF NOT EXISTS "ContactMailConsent_basis_idx"
    ON "ContactMailConsent"("basis");

-- contactPageId は「正規化済み（ハイフン無し・小文字の32桁hex）」で入る約束。
-- ContactMailPreference と同じ制約を張り、`ABC-DEF…` と `abcdef…` が
-- 別レコードとして入ることを DB 側で防ぐ。
ALTER TABLE "ContactMailConsent"
    DROP CONSTRAINT IF EXISTS "ContactMailConsent_contactPageId_normalized";
ALTER TABLE "ContactMailConsent"
    ADD CONSTRAINT "ContactMailConsent_contactPageId_normalized"
    CHECK ("contactPageId" ~ '^[0-9a-f]{32}$');

-- 証跡が空の根拠は根拠として役に立たない（後から誰も裏を取れない）。
-- 画面側でも必須にしているが、DB でも空文字を弾く。
ALTER TABLE "ContactMailConsent"
    DROP CONSTRAINT IF EXISTS "ContactMailConsent_evidence_not_blank";
ALTER TABLE "ContactMailConsent"
    ADD CONSTRAINT "ContactMailConsent_evidence_not_blank"
    CHECK (btrim("evidence") <> '');

-- 送信根拠の照合は lower("contactEmail") で行う（src/bulk_email/db.py）。
-- 単純な index では効かないため、式インデックスを張っておく。
CREATE INDEX IF NOT EXISTS "ContactMailConsent_contactEmail_lower_idx"
    ON "ContactMailConsent"(lower("contactEmail"));

-- ついでに配信停止側にも同じ式インデックスを張る（設計メモの申し送り10番）。
-- src/bulk_email/db.py の fetch_opt_outs も lower("contactEmail") で照合しており、
-- 既存の "ContactMailPreference_contactEmail_idx" は使われない。
CREATE INDEX IF NOT EXISTS "ContactMailPreference_contactEmail_lower_idx"
    ON "ContactMailPreference"(lower("contactEmail"));
