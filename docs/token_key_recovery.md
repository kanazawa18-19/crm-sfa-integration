# Vercelの既存暗号化キーを安全に引き継ぐ

2026-09-08 主機CX。状態：ローカル準備。外部への回収用デプロイは未実施。

## 確認した事実

- CRM本体とダッシュボードの `TOKEN_ENCRYPTION_KEY` はAPI実測で両方 `type=sensitive`、対象 `production`、値は返らない。通常の環境変数取得では引き継げない。
- ダッシュボードはプロジェクト `prj_xBiFEQcP7NlshpK6tyh4gSCQeamj`、チーム `team_RSiK5xUClGdmoKtS9OXUf4M1`。rootDirectoryはnull。
- 本番ID `dpl_9SG7CfsTHLSrZroSEjBkPjjtVMUm`。既存aliasは `crm-sfa-integration-dashboard.vercel.app` と `crm-sfa-integration-dashboard-cnctor.vercel.app`。実行直前に読み直す。
- Vercel認証による保護は `all_except_custom_domains`。回収先の固有URLにも保護が掛かることを実測するまで回収しない。

## 選んだ方法

このMacだけに回収用RSA秘密鍵を保持し、公開鍵だけをVercelへ渡す。
ビルド処理が本番設定の `TOKEN_ENCRYPTION_KEY` をRSA-3072以上・OAEP SHA-256で包み、暗号文だけを成果物へ書く。
アプリの通常処理、DBアクセス、Gmail送信、cronは含めない。
鍵はログ・ソース・会話・Vaultへ出さない。新しい鍵への交換でもない。

`scripts/key_recovery/seal.mjs` と `vercel.template.json` はそのための素材。
RSA秘密鍵はローカルプロセスのメモリだけに保持し、終了時に失われる設計で運用する。
元のアプリ鍵はDBの既存Gmail暗号文1件を復号できることを確認した後、macOSキーチェーンに保存する。
Secret Managerへの反映は別の操作として扱う。latestを更新するとCloud Runの新規インスタンスが新しい版を読むため、安易に版を追加しない。

## 実行時の手順

1. 本番IDとalias、環境変数メタデータ、デプロイ保護を再取得する。
2. `mktemp -d` で一時アップロード専用ディレクトリを作る。置くのは `seal.mjs`、テンプレートを改名した `vercel.json`、公開鍵 `recovery-public.pem`、上記プロジェクトID・チームIDだけを含む `.vercel/project.json` の4ファイル。既存リポジトリや秘密鍵、`.env` はコピーしない。
3. 本人の実行許可後に、そのディレクトリから `vercel deploy --prod --skip-domain --yes`。`--skip-domain` は必須。`promote` や `alias set` は行わない。実行結果から作成したデプロイIDと固有URLを保存する。
4. 本番ID・aliasが変わらず、一時デプロイが本番ドメインを持たないことをAPIで確認。不一致なら回収せず停止する。
5. 固有URLの認証なしアクセスが拒否されることを確認。その後ログイン済みCLIの `vercel curl` で `/recovery.json` を取り、ローカルメモリ内で開く。公開リンクや認証バイパスリンクは作らない。
6. 復号したアプリ鍵で既存Gmail暗号文1件を読み取り専用で検証。対象0件・復号失敗なら保存せず停止。成功した場合だけmacOSキーチェーンへ保存（値を引数やログへ出さない方法を実装してから行う）。
7. 本番IDとaliasを再確認し、今回作ったIDのデプロイだけを削除する。失敗時も作成済みIDを確認して後片付けする。既存デプロイは消さない。保管場所と検証件数だけをVaultへ残す。

外部デプロイ・回収・キーチェーン保存・削除の一連の実行は未検証で、操作用のローカル処理は実行前に組む。
ローカルの暗号処理テストは `node --test scripts/key_recovery/seal.test.mjs`。
これはRSAでの包み方の確認であり、Vercel本番鍵の回収成功を意味しない。

## なぜ実行前に止めるか

本人の共通ルール「本番デプロイは指示があるまで行わない」に従う。
通常の本番URLは切り替えないが、productionの環境変数を使うデプロイであるため、準備後にその具体的な操作の許可を取る。

## 公式の根拠

- Sensitive値の読み戻し制限：https://vercel.com/docs/environment-variables/sensitive-environment-variables
- ビルド・実行時の環境変数：https://vercel.com/docs/environment-variables
- 本番ドメインを切り替えない待機用デプロイ：https://vercel.com/docs/cli/deploying-from-cli
