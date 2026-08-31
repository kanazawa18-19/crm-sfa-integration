-- Googleログインの識別子として OIDC subject を保持する列を追加する(2026-08-31)。
--
-- 【なぜ必要か】
-- Googleログインは当初 email だけでUserを照合していた。Google Workspace では
-- 退職者のアカウントを削除したあと、**同じメールアドレスで別のGoogleアカウントを
-- 作り直せる**。その場合 verified_email も true になるため、email だけの照合では
-- 別人が旧ユーザーとしてログインできてしまう(2026-08-31、ChatGPTのレビュー指摘)。
-- Google自身も、OIDCの識別子には変わりうる email ではなく sub を使うよう案内している。
--
-- 【expand-only。窓が開かない理由】
-- `prisma migrate deploy` はビルド時に走るため、新デプロイが公開されるまでの数十秒は
-- 「1つ前の本番コード」が動く。この列は**追加だけ**で、NULL許容・既定値なし。
-- 1つ前のコードはこの列を読み書きしないので、窓の間も何も壊れない。
-- 逆にNOT NULLで入れると、旧コードのINSERT(ユーザー招待)が全部落ちる。
--
-- 【バックフィルしない理由】
-- 既存ユーザーの sub は、そのユーザーが一度Googleでログインするまで分からない。
-- 初回ログイン時に束縛する(trust on first use)。それまではNULLのまま、
-- 従来どおり email で照合する。
--
-- 【元に戻す場合】
-- DROP INDEX "User_googleSubject_key";
-- ALTER TABLE "User" DROP COLUMN "googleSubject";
ALTER TABLE "User" ADD COLUMN "googleSubject" TEXT;

-- 1つのGoogleアカウントが複数のUserに紐づかないようにする。
-- NULLは重複可(Postgresの一意制約はNULL同士を衝突とみなさない)なので、
-- 未ログインのユーザーが何人いても問題ない。
CREATE UNIQUE INDEX "User_googleSubject_key" ON "User"("googleSubject");
