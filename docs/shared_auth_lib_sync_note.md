# 認証・セキュリティ関連libのweb-engagement-tool同期メモ

`dashboard/lib/` の以下5ファイルは、2026-08-15にweb-engagement-tool(`src/lib/`)から
確信犯的に移植したコードで、ロジックはほぼ同一。npm workspace等のモノレポ構成には
なっておらず自動同期の仕組みが無いため、**どちらか一方にセキュリティ修正・バグ修正を
入れたら、もう片方も確認する**こと。

| ファイル | crm-sfa側の既知の差分(意図的) |
|---|---|
| `lib/adminSession.ts` | 環境変数名 `SESSION_SECRET`(web-engagement-toolは`ADMIN_SESSION_SECRET`)。`COOKIE_NAME`/`PENDING_2FA_COOKIE_NAME`をexportしている点も差分。 |
| `lib/twoFactor.ts` | TOTPの`ISSUER`定数がプロジェクト名に合わせて`"crm-sfa-integration-dashboard"`。 |
| `lib/tokenCrypto.ts` | 環境変数名 `TOKEN_ENCRYPTION_KEY`(web-engagement-toolは`CALENDAR_TOKEN_ENCRYPTION_KEY`)。 |
| `lib/ipAllowlist.ts` | コメントの参照パスのみ差分(`proxy.ts`)。`ALWAYS_ALLOWED_IPS`の中身(社内拠点IP)は同一。 |
| `lib/auth.ts` | **web-engagement-tool側とロジックが分岐している唯一のファイル**。web-engagement-tool側は監査ログのactor解決に`AsyncLocalStorage`を使っていたが本番バグ(`03322f5`、[[feedback_nextjs_asynclocalstorage_server_actions]])で撤去済み。crm-sfa-integration/dashboard側の監査ログはPythonバックエンドが直接書き込む方式で、そもそも`setAuditActor()`のようなコードを持ったことが無い(2026-08-18確認、`grep`で該当パターン無し)。つまりこの分岐は問題ではなく、今後もauth.tsだけは同一である必要はない。 |

2026-08-18、監査ログ実装以降のリファクタリング検討(タスク#7)の一環で、両リポジトリの
重複実装を棚卸しした際に作成。共通npmパッケージへの切り出しは、2つの独立したVercel
プロジェクト・GitHubリポジトリという構成上コストが大きいため見送り、まずはこの
比較表による手動同期の徹底を優先した。
