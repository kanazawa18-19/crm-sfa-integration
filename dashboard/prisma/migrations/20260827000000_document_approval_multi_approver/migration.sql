-- 見積書 承認フロー: 承認者を複数選択して同時送信できるようにする(2026-08-27)。
--
-- expand方式(追加のみ)のマイグレーション。旧カラム"approverEmail"はこのマイグレーションでは
-- DROPしない。理由: `prisma migrate deploy`はビルド時に走るため、新デプロイが公開される前に
-- 本番DBのスキーマだけが先に変わる。その数十秒の間は旧コードが動いており、旧カラムを消すと
-- その窓で承認リクエスト送信とポーリングcronが500になる(このプロジェクトは過去に同期基盤で
-- 実害のある事故を複数回起こしているため、無停止で進められる形を優先する)。
-- "approverEmail"は安定後に別マイグレーションで削除する(docs/quote_approval_note.md参照)。

-- AddColumn: approverEmails(正)を追加する。nullableのまま追加する。
ALTER TABLE "DocumentApproval" ADD COLUMN "approverEmails" TEXT[];

-- Backfill: 既存行を旧"approverEmail"(単一)から1要素配列で埋める。
UPDATE "DocumentApproval" SET "approverEmails" = ARRAY["approverEmail"] WHERE "approverEmail" IS NOT NULL;

-- 【意図的にNOT NULL制約を付与しない】(shirokuma-secレビューBLOCKER対応)
-- `prisma migrate deploy`はNext.jsのビルド時に走るため、新デプロイが公開される前の数十秒は
-- 旧コード(このマイグレーション追加前の`insert_document_approval()`)がまだ本番で動いている。
-- 旧INSERT文は列を明示指定しており"approverEmails"には一切触れないため、ここでNOT NULL
-- 制約を付けると、その窓で送信される承認リクエストが
-- `null value in column "approverEmails" violates not-null constraint`で失敗し、
-- 承認リクエスト送信が500を返す(旧カラムをDROPして起きる事故を、ADD側のNOT NULL化で
-- 作ってしまうことになる)。読み取り側(src/document_generation/approval_db.py)は、この窓で
-- 作られた「approverEmailsがNULL・approverEmailのみ埋まった」行を読めるようフォールバックを
-- 持つ(同ファイル参照)。NOT NULL化は新コードが本番で安定稼働してから
-- (目安: 事故なく数営業日稼働)、別マイグレーションで行う
-- (docs/quote_approval_note.md「旧approverEmail列を削除できる条件」と同様の考え方)。

-- AlterColumn: 旧"approverEmail"はnullableへ変更する(経過措置のdual-write専用カラムとして
-- 残すのみで、新規コードは読み取りに使わない)。
ALTER TABLE "DocumentApproval" ALTER COLUMN "approverEmail" DROP NOT NULL;
