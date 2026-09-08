# Gmail通知期限更新の配備前実サービス検証

2026-09-08 主機CX。基準コード `40fd685`。本イシューは未完了。
本番配備・本番run・業務Gmailの通知設定変更は行っていない。

## 実測結果

| 対象 | 結果 |
|---|---|
| ローカルPostgres | Homebrewで17.11を導入。使い捨てクラスタをUnixソケットのみで起動、独立QA後に停止 |
| 隔離スイート | 19 passed / 1 deselected / 1 warning、0.83秒。19件中18件が実DB fixtureを使用、1件はDB不要 |
| Google通信・復号 | 偽応答のまま。実Gmailの連携許可・watch実行は未実施 |
| Pub/Sub | 検証topicとpull subscriptionを作成・読み取り照合済み。Gmailの発行権限は未付与 |
| Cloud Logging | 製品ログの到達・検索は未検証 |
| 監視 | 失敗／終了欠落／HTTP500の通知設定は未実施 |
| Google連携設定 | 対象GCPのGoogle Auth Platformは未構成。作成フォームのアプリ名入力まで。保存未実施 |

## Postgres検証の方法と限界

`tests/gmail_sync/test_watch_result_isolated.py` の一時コピーで、SQLite接続・DDL・seed・
結果照合を実psycopgへ変更。製品のSQLとHTTP/ログ照合は維持し、接続の終了処理を
psycopg本体へ委譲した。Google要求と復号は原本と同じ偽物。外部ソケット禁止も維持。
接続先は専用Unixソケットに固定し、本番DATABASE_URLは読まない。

独立担当 `kuma-qa` がコードを読み取り、次を実行した。

```sh
PYTHONPATH=/Users/cnctor/crm-sfa-integration .venv/bin/python -m pytest \
  /private/tmp/test_crm_watch_postgres_trial.py -q -k 'not hard_kill'
```

- 初回historyId保存、既存historyId維持、保存行数とHTTP／結果ログの一致を確認。
- commit直前の人工例外で実Postgresがrollbackし、後続担当が保存されることを確認。
- 更新対象0件を成功にしないこと、保存後例外／ログ失敗ではHTTP500と保存済みが並ぶことを確認。
- `TIMESTAMP(3)`のタイムゾーンなし日時の判定を確認。
- 強制終了テストは実DBを使わないため今回除外。前イシューの検証結果を拡張したとは扱わない。
- 最小5列のテーブル。Prisma全スキーマ・マイグレーションは未検証。
- `db._connect`を差し替えた試験。本番接続設定・Neon・接続プールは未検証。
- commitのサーバー側拒否／応答喪失は未検証。一部テストに取得順序の前提が残る。
- 非推奨警告1件はTestClientのhttpx利用。失敗ではない。

一時コードとクラスタは `/private/tmp` にあり永続成果物ではない。
コード `/private/tmp/test_crm_watch_postgres_trial.py`、変換手順
`/private/tmp/prepare_crm_postgres_trial.py`、停止済みクラスタ
`/private/tmp/crm-gmail-pg-0ckhq2j0/data`。再開時は存在を確認する。
Homebrewの既定クラスタも作成されたが未起動。自動起動サービスは登録していない。

## 検証用の通知受け口

GCP `fabled-electron-406310`。

| 設定 | 実測値 |
|---|---|
| topic | `projects/fabled-electron-406310/topics/crm-gmail-watch-trial` |
| subscription | `projects/fabled-electron-406310/subscriptions/crm-gmail-watch-trial-pull` |
| pushConfig | `{}`（pull方式。本番Webhookへの送信先なし） |
| メッセージ保持 | 86,400秒（1日） |
| 未使用時の期限設定 | 604,800秒（7日） |

topicは自動削除ではない。検証終了後の片付け対象として残す。
Gmail由来の通知到達は未確認。本人の指定した検証GmailはVaultに記録し、コードへ埋め込まない。

## 保留になった設定

自動承認審査が次の2操作を拒否。迂回・再試行はしていない。

1. 本人の業務アドレスをGoogle Auth Platformのサポート連絡先に選択する操作。
   サポート連絡先としてGoogleへ登録する明示承認不足との理由。
2. `gmail-api-push@system.gserviceaccount.com` へ、検証topicだけの
   `roles/pubsub.publisher` を付与する操作。宛先・権限・対象を指定した明示承認不足との理由。

Gmail通知にはこのGoogle管理主体の発行権限が必要。OAuth実行プロジェクトとtopicの
プロジェクトも一致させる必要がある。成功したwatchの直後に通知が発生するので、
テストメール送信をせずに通知到達を確認する。

出典：[Google公式のGmail通知設定](https://developers.google.com/workspace/gmail/api/guides/push?hl=ja)、
[watch APIのtopic制約](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users/watch)。

## 次の手順

1. 保留2操作について本人の承認を得る。検証用OAuthは外部・テスト段階で用意し、本人指定の
   個人Gmailのみテスト利用者にする。本番CRM/MAの既存OAuthは変更しない。
2. 検証用デスクトップOAuthクライアントを用意し、ローカルの受け取り口で本人の同意を取得。
   クライアント秘密値／refresh tokenはキーチェーンへ保存し、会話・Vault・Gitへ出さない。
3. Gmail専用の実Postgres検証を実施。初回登録→期限余裕のスキップ→期限を検証DBだけで戻して
   再更新を別ケースとして照合する。本文取得目的の再runをしない。
4. 隔離した実行先から製品JSONログがCloud Loggingへ入ることを確認し、run_id／sequenceで照合。
5. 専用サービスを対象に失敗・HTTP500・開始/終了突合による終了欠落の監視を整え、本人だけの
   通知先への到達を試験する。監視宛先の指定はまだ確定扱いにしない。
6. 必要レビューと引き継ぎ記録を完了。本番配備・本番runは別途指示後。
