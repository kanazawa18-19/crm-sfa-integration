# crm-sfa 脆弱性点検 2026年10月23日

対象はこのリポジトリの管理画面・API・CI。他プロジェクトは対象外。本番変更は行っていない。

## 確認した構成と判定

| 対象 | 実測・根拠 | 判定 |
|---|---|---|
| PHP・Laravel | Git管理ファイルのPHP・Composer関連は0件。APIはPython/FastAPI（Dockerfile）、管理画面はNext.js | 管理対象構成には該当なし。ホスティング事業者内部の構成は対象外 |
| Apache | 管理対象のApache/httpd関連ファイル0件。公開先はVercel | 自前Apacheの更新作業は該当なし |
| Next.js | 本番の既存ビルドログは16.3.0。App RouterとServer Actionsを使用 | 10月14日の追加勧告への更新が必要 |
| React | 更新前19.2.8。RSCはNext.js同梱版 | React単独のバージョンだけで判定せずNext.jsを更新 |
| Node.js | 手元の既定22.18.0、本番設定24.x。CIは22とlts/* | 専用検証環境・CIを24.22.1へ統一。本番パッチ番号は未確認 |
| TLS | 管理画面/APIの既存公開ドメイン2件を証明書検証付きで接続 | ホスト名・信頼チェーン・期限検証成功、TLS 1.3 |

本番管理画面は2026-10-19 17:21 JST作成のReady配備。既存ログの`Detected Next.js version: 16.3.0`を確認した。ローカルの修正を本番適用済みとは扱わない。

## 公式勧告との照合

- [Next.js 2026年10月14日勧告](https://nextjs.org/blog/nextjs-security-update-october-14-2026)：16系の修正版は16.4.1以上。Server ActionsのDoSはこのアプリの構成条件に該当する。Cache Componentsは設定されておらず、`use cache`も検出しなかったが、これを更新不要の根拠にはしない。画像関連はsharpも含め更新する。
- [React Server FunctionsのDoS勧告](https://github.com/react/react/security/advisories/GHSA-h3gv-c5rc-p453)：RSCの19.2系列は19.2.9が修正版。Next.js内部のRSCをoverridesで独自差し替えせず、Next.js公式修正版を使用する。
- [Node.js 2026年10月21日勧告](https://nodejs.org/en/blog/vulnerability/october-2026-security-releases)：24系は24.22.1が修正版。ローカル検証用アーカイブは公式SHASUMS256とSHA256一致を確認した。
- [Vercel Node.js勧告](https://vercel.com/kb/bulletin/node-js-security-advisory)：24.xのパッチはVercel管理。閲覧時は順次展開中との説明で、WAFだけでは外部取得レスポンスの危険を防げない。本番実プロセスのパッチ番号確認は残る。
- [Vercelの証明書管理](https://vercel.com/docs/domains/working-with-ssl)：公開証明書はホスティング側で管理。Certbotの導入・操作は行わない。

## 証明書の実測

| 接続先 | 有効期限（UTC） | SAN | 接続 |
|---|---|---|---|
| crm-sfa-integration-dashboard.vercel.app | 2026-11-27 19:48:08 | *.vercel.app | TLS 1.3・検証成功 |
| crm-sfa-integration.vercel.app | 2026-11-27 19:48:08 | *.vercel.app | TLS 1.3・検証成功 |

最初のPython既定CAストアでは発行元不足になったため、macOSの`/etc/ssl/cert.pem`で再検証した。検証無効化はしていない。業務HTTP処理や攻撃リクエストは送っていない。TLS 1.0/1.1の拒否、全暗号スイート、更新失敗通知、ホスティング内部の秘密鍵管理は未検証。

## 更新と検証

更新前のnpm監査は20パッケージ（重大1・高17・中2）。これはCVEの件数ではなく、依存経路を含む警告対象パッケージ数。

| 項目 | 変更前 | 変更後 |
|---|---|---|
| Next.js / eslint-config-next | 16.3.0 | 16.4.2 |
| React / React DOM | 19.2.8 | 19.2.9 |
| sharp（Next.jsの依存） | 0.35.3 | 0.35.5 |
| Nodemailer | 9.0.5 | 10.2.1 |
| Prisma / client / adapter-pg | 7.9.1 | 7.10.1 |
| Vitest（lockfile） | 4.1.10 | 4.1.12 |
| 本番用npm監査 | 更新前は全依存のみ採取 | 警告0 |
| 全依存npm監査 | 重大1・高17・中2 | 高5 |

残る高5は`braces → micromatch → fast-glob → @next/eslint-plugin-next → eslint-config-next`の同じ開発用経路。監査の対象はGHSA-vfj7-8cjw-p6xmで、深く入れ子になったglobパターンによる処理停止。npm公式レジストリのbraces最新版も3.0.3で、修正版はまだ取得できない。外部から受け取ったglobを検査ツールへ渡さず、修正版公開後に再点検する。`npm audit fix --force`が提案したeslint-config-next 14.2.35への逆行や、未検証の独自差し替えは見送った。

`npm audit fix --dry-run`で変更予定を確認後、`--force`なしで適用。Prisma関連はCLIと実行ライブラリの版を揃えた。ロックファイルを更新し、本番依存監査をCIへ追加。`.node-version`を両Node.jsジョブから読み、クリーン環境で必要な`next typegen`を型検査前に追加した。

最終構成・Node.js 24.22.1で管理画面47ファイル378試験成功。実MailComposerのMIME生成も既存試験で検証したが、外部SMTP・実メール配送は行っていない。GAS試験103件成功。型検査はルート型生成後に成功（生成前のPageProps/LayoutProps不足を解消）。`next build --webpack`も成功（TypeScript検査・静的ページ40件生成）。本番既定のTurbopackビルドと、認証済みの実業務操作は未検証。

共有の既定Node.js 22.18.0は他プロジェクトに影響するため変更していない。crm-sfaのローカル作業にも24.22.1以降の対応版を明示的に選ぶ必要がある。Vercelの24.x設定から実パッチ適用済みとは断定しない。

今回は依存更新とCIの小修正であり、独立セキュリティレビューを実施。3体並列・他社モデル2社の実装レビューは省略する。

## 本番反映時の条件

1. このリポジトリはmainへのpushで本番配備される。未pushの先行変更も含め、本人の本番反映指示を受けてから実行する。
2. 通常の`npm run build`には`prisma migrate deploy`が含まれる。今回の検証では実行せず、機密設定を除外したコピーでクライアント生成・テスト・型検査・Next.jsビルドを個別に行う。
3. 既存ハブにNeon利用枠超過とDB移行待ちの記録がある。今回本番DBへの接続・移行・有料化は行っていない。更新配備を成功扱いにする前に、この既存の配備阻害要因を解消する必要がある。
4. 配備後のNext.js修正版、Node.js実パッチ、認証・API・メールの必要な隔離検証を確認する。
5. 過去の侵害有無は未判定。Git上の管理画面初出は2026-08-06で、2025年12月の未修正公開を示す証拠は今回得ていない。これだけで侵害なしとはしない。2026年の脆弱な公開期間について保存ログ・WAF記録等を調べ、侵害兆候や秘密情報の露出が判明した場合に認証情報交換を行う。

## ローカルでの実HTTP確認

機密設定を除いた検証コピーを127.0.0.1:3187で起動。DBなしでは共通認証処理のAppSettings参照が失敗し500になったため、127.0.0.1:55459の使い捨てPostgreSQLへPrismaスキーマを作成して再検証した。ログイン画面200・HTML生成、未ログインの/usersは307で/loginへ転送、POST /api/sync-reviewも共通認証処理で307転送を確認。APIルート単体の401とは違い、実HTTPでは先行する共通処理が転送する。業務API・Google認証・メール送信先への接続は行っていない。

検証サーバーと一時PostgreSQLは停止済み。独立セキュリティレビューは最終差分と監査証跡を確認し、新規BLOCKER/WARNなし。既知の開発用依存5件が解消したという意味ではない。

## 2026-11-12 追補：本番依存のZod警告を修正

11/12のCI `43154913744` は管理画面の本番依存検査で失敗し、後続テスト・型検査は未実行だった。ローカルで同版の依存を監査し、`@vercel/blob → @vercel/oidc → @vercel/cli-config → zod` の経路に高4件を再現した。直接の原因はZod 4.1.11で、公式勧告は [GHSA-mrhx-c7xc-m3w5](https://github.com/advisories/GHSA-mrhx-c7xc-m3w5)（4.6.6で修正）。

- `dashboard/package.json` に `@vercel/cli-config` 配下だけのZod 4.6.7指定を追加し、ロックを更新した。Blob本体・OIDC・Next.jsの版は変更していない。旧版への強制巻戻しや検査除外は行っていない。
- Zodは開発用の既存要求範囲とも一致するため、ロック上は4.6.7へ共有される。上流の依存指定が修正された際に限定指定の除去を検討する。
- 修正後の本番依存監査は全重要度0件。全依存では開発用minimatch経路の中7件が残る。`npm audit fix --dry-run` は14パッケージの更新を提案したが中7件は残るため適用していない。上記10/23時点のbraces高5件を現在値として流用しない。
- Node 24.22.1・環境変数ファイルを除外した隔離コピーでクリーン導入、Prisma生成、既存テスト378件、型生成・型検査、webpackビルドが成功。lintはエラー0・警告26件。`npm run build`に含まれるDBマイグレーションは実行していない。
- 実ライブラリの設定スキーマで正常値・不正値・未知キー保持を検証し、Blob/OIDCの読込も成功。独立レビューはBLOCKER/WARNなし。本番Blob通信・認証済み業務操作・本番反映後のCIは未検証。
- 初回ビルドはネットワーク制限によるGoogle Fonts取得失敗とディスク容量不足を記録。今回作った失敗キャッシュと展開済みアーカイブを削除し、ネットワーク許可付きの再試行で成功。外部データ・共有環境の設定は変更していない。

証跡：私有 `~/.local/state/crm-sfa-integration/` の `npm-audit-20261112-before.json` / `npm-audit-20261112-after.json` と `dependency-check-20261112/` 内の監査・試験・型検査・lint・ビルドログ。秘密値を除いた空の環境と接続不能なローカルダミーDB URLで検証した。

### 同日続行：開発用依存とlint警告の解消

本人の「それもこのセッションで続けろ」を受け、残警告修正とpush・本番反映確認まで続行した。

- 開発用minimatchの [GHSA-g3hv-75wg-m249](https://github.com/advisories/GHSA-g3hv-75wg-m249) は11.0.1で修正。上流typescript-estreeはまだ10系を要求するため、使用箇所を確認し同パッケージ配下だけ11.0.1に固定した。上流が対応したらこの一時指定を除去する。
- Node24.22.1との互換性、実際の呼出し `minimatch(filePath, pattern, { dot: true })`、旧新版の268パス×12パターン＝3,216比較で差分0を独立レビューで確認した。全依存監査は全重要度0件。
- 使われないimport・定数・テスト補助関数を削除。Server Action等で未使用を明示した `_` 始まりの引数は、呼出し仕様を保つため残した。lint設定はその引数だけを対象外とし、未使用変数・importの検査は維持。lintはエラー・警告とも0件。
- CIに開発用を含む中以上の脆弱性検査と警告0件のlintを追加。隔離コピーの既存378試験は全成功。独立レビューBLOCKER/WARNなし。
- 配備先の現在確認で、API・管理画面とも `cnctor-c2cc0c84` にあることをCLIで確認。旧チーム `cnctor1` の案内をプロジェクトAGENTS.mdで訂正。既存の認証・接続設定を変更せず明示的に対象を指定する。

最終ビルド・配備・CIの結果はVaultと共有WBSへ記録する。証跡は私有 `release-check-20261112/`。

### 本番確認で見つかったDB暗号化設定の警告

`1cccf3d`の本番反映後、管理画面ログにSSL別名の将来変更警告が3件あった（`/login`は200、`/`は307）。pg-connection-string 2.14.0ではlibpq互換指定がない`prefer`・`require`・`verify-ca`を`verify-full`と同じ扱いにしているが、将来のメジャー更新で意味が変わる旨の警告である。

- `lib/databaseConnectionString.ts`で、この3つの別名だけを`verify-full`へ明示する。DB接続情報やVercelの環境変数は変更しない。明示的なlibpq互換設定、TLS無効化設定、その他の接続方式はそのまま渡す。
- `lib/prisma.ts`の実行時アダプター生成へ適用。接続先・認証情報・ポート・DB名・追加パラメーター保持と、pgが採用する重複クエリの最後の値の扱いを実パーサーでテストする。接続先や秘密値をエラーへ出さない。
- 独立レビューでraw空白・不正な%を含む特殊形式の正規化に差が見つかったため、その形式は原文をpgへ渡すよう修正。架空140組で旧解釈との差分0、追加回帰テストで確認した。この例外形式では警告が残り得るが、接続情報の解釈を優先して維持する。
- 現行の証明書・ホスト名検証を維持するための修正であり、検証を無効化して警告を隠す対処ではない。

証跡は私有 `tls-mode-check-20261112/`。最終本番版とCI・読取確認はVault/WBSへ記録する。
