-- 見積書 承認フロー: "approverEmails"(複数承認者、正)をNOT NULL化する(2026-08-28)。
--
-- expand/contractの contract 第1段階。20260827000000_document_approval_multi_approver で
-- 意図的に見送ったNOT NULL化を、新コードが本番稼働したいま適用する。
--
-- 【なぜ今なら安全か】
-- `prisma migrate deploy`はビルド時に走るため、新デプロイが公開されるまでの数十秒は
-- 「1つ前の本番コード」が動く。前回のマイグレーション時点では、その1つ前のコードが
-- "approverEmails"に一切触れないINSERT文を持っていたため、NOT NULLを付けるとその窓の
-- 承認リクエスト送信がnot-null違反で500になった。いまは1つ前の本番コードが
-- 複数承認者対応済み(2c83340、2026-08-27デプロイ)で必ず"approverEmails"を書くため、
-- この窓で違反が起きない。
--
-- 【なぜ先にバックフィルするか】
-- 前回のデプロイ窓でINSERTされた行は"approverEmails"がNULLのまま残っている可能性がある。
-- NULLが1行でも残っているとSET NOT NULLが失敗し、`prisma migrate deploy`ごと失敗して
-- **ビルド全体が落ち、全ルートが障害になる**(過去にweb-engagement-toolで発生)。
-- そのため同じマイグレーション内で必ず先に埋める。旧"approverEmail"もNULLの行は
-- 空配列にする(NULLを残さないことを最優先する。承認者0人の行はUI上その旨が表示される)。

-- Backfill: NULLが1行も残らないようにする。
UPDATE "DocumentApproval"
SET "approverEmails" = CASE
        WHEN "approverEmail" IS NOT NULL THEN ARRAY["approverEmail"]
        ELSE ARRAY[]::TEXT[]
    END
WHERE "approverEmails" IS NULL;

-- AlterColumn: "approverEmails"をNOT NULL化する。
ALTER TABLE "DocumentApproval" ALTER COLUMN "approverEmails" SET NOT NULL;

-- 【旧"approverEmail"列はここでは削除しない】
-- このマイグレーションと同時にDROPすると、上記の窓で動いている1つ前の本番コードが
-- SELECT文で"approverEmail"を明示指定しているため、列が消えた瞬間に承認リクエストの
-- 参照系とポーリングcronが500になる。DROPは「この列を読み書きしないコードが本番稼働して
-- から」次のマイグレーションで行う(docs/quote_approval_note.md参照)。
