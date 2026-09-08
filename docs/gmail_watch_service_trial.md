# Gmail通知期限更新の配備前実サービス検証

2026-09-08 主機CX。基準コード `40fd685`。本イシューは未完了。
本番配備・本番run・業務Gmailの通知設定変更は行っていない。

## 実測結果

| 対象 | 結果 |
|---|---|
| ローカルPostgres | Homebrewで17.11を導入。使い捨てクラスタをUnixソケットのみで起動、独立QA後に停止 |
| 隔離スイート | 19 passed / 1 deselected / 1 warning、0.83秒。19件中18件が実DB fixtureを使用、1件はDB不要 |
| Google通信・復号 | 偽応答のまま。実Gmailの連携許可・watch実行は未実施 |
| Pub/Sub | 検証topicとpull subscriptionを作成・読み取り照合済み。Gmailの発行権限を検証topicだけに付与・照合済み |
| Cloud Logging | 専用bucket・sink・非公開Cloud Run作成済み。起動ログ2件の専用bucket到達・検索を確認。製品結果ログは未検証 |
| 監視 | 失敗／終了欠落／HTTP500の通知設定は未実施 |
| Google連携設定 | 外部Testingアプリ・デスクトップclient・指定Gmail1件を設定済み。本人の同意待ち |

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

## 権限と認証の状況

サポート連絡先登録と検証topicへのGmail発行権限は、本人の明示承認後に設定・再読取済み。
アプリ名は「CRM Gmail 隔離検証」、外部Testing、指定Gmail1件だけがテスト利用者。
有効なデスクトップclientは `crm-gmail-watch-trial-desktop-v2`。
秘密値はmacOSキーチェーンに格納し、ダウンロードJSONは削除済み。
最初の未使用clientはブラウザDOMのコピー用ラベルに秘密値が含まれ、ツール出力へ
露出したため直ちに削除した。削除後に一覧0件を確認してからv2を作成した。
露出したclientは使わない。v2の秘密値は出力していない。

本人のGoogle同意は未完了。専用のローカル受け取り口はPKCE・state・1回限りcallbackを
使い、gmail.metadataだけを要求する。profileで指定Gmail一致を確認してからrefresh tokenを
キーチェーンへ保存する。実Gmail更新はまだ1回も行っていない。

自動承認審査が検証SAのログ閲覧権限を拒否したため、次の限定操作について本人へ確認中。

- 検証bucketの `_AllLogs` viewだけに `roles/logging.viewAccessor`。
- 同名検証Cloud Runだけに `roles/run.invoker`。
- 本人から検証SAのID tokenだけを発行する `roles/iam.serviceAccountOpenIdTokenCreator`。
- 本人宛ての監視テスト通知。通知チャネルやアラートは未登録。

プロジェクト全体のログ閲覧権限へ広げない。本番SA・本番サービスのIAMは変更しない。

Gmail通知にはこのGoogle管理主体の発行権限が必要。OAuth実行プロジェクトとtopicの
プロジェクトも一致させる必要がある。成功したwatchの直後に通知が発生するので、
テストメール送信をせずに通知到達を確認する。

出典：[Google公式のGmail通知設定](https://developers.google.com/workspace/gmail/api/guides/push?hl=ja)、
[watch APIのtopic制約](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users/watch)。

## 隔離ログサービス

- `scripts/cloud_run/gmail_trial/` に合成ログ専用サービスを追加。Gmail・DB・Secretへ接続しない。
- 製品の `watch_result.py` と `cron_result_log.py` を無変更で利用する最小5ファイルをビルド。
- GCPのservice・SA・ログbucket・sinkはすべて `crm-gmail-watch-trial`。
- bucketはglobal、保持7日。sinkは検証service_nameだけを抽出する。
- Cloud Runはus-east4、非公開、最小0・最大1、同時4、HTTP制限60秒。
- 現在のrevisionは `crm-gmail-watch-trial-00002-ftm`。時間予算・件数上限・安全なエラー分類を含む修正版。
- `/api/healthz`、合成4ケース、全ページ取得後の開始/終了突合を用意。
- 検索40秒予算は前後チェック。通信中の厳密な強制停止は保証しない。
- 独立QAは新規37件＋既存31件＝68件成功。実クラウドの到達検証とは別。
- アラートは業務失敗・HTTP500・終了欠落・監視取得失敗に加え、監視自身の
  `check_completed`欠落も必要。2026-09-08の既存Monitoringポリシーは実測0件。

## 次の手順

1. 本人のGoogle同意と限定権限の回答を受け取る。秘密値はチャットへ送らない。
2. 修正版の独立レビュー・Gemini Pro／Claude Opus 5高レビューを完了。
   初回Geminiは回答拒否、修正後の最終レビューではBLOCKER/WARN/INFO各0件。Claudeも最終BLOCKER 0。WARNの採否は下記。
3. 実Postgresに検証専用DBを新規作成し、実Gmailの初回登録・期限余裕のスキップ・
   検証DBだけの期限変更後の再更新を別ケースとして実施する。一度きりの実行印を
   原子的に作成し、途中失敗しても消さない。本文取得目的の再runをしない。
4. Pub/Subの通知到達と保存結果を照合。メール本文は取得しない。
5. 反映済み検証revisionで製品JSONログの到達・run_id／sequenceを照合。
   専用viewの閲覧成功と対象外viewの拒否を確認する。
6. アラート・監視自身の欠落監視を設定し、本人だけの通知先で到達確認。
7. レビュー・実測・引き継ぎを記録。本番配備・本番runは別途指示後。

## 2026-09-08の検証サービス反映

修正版5ファイルだけを送信し、Cloud Build内の `import app` に成功。
非root（65532）指定のrevisionがReadyになり、起動時TCP確認も1回で成功。
起動ログ2件を専用bucketの `_AllLogs` viewから検索できた。実行者は既存gcloud利用者であり、
検証SAのview限定権限が通る証拠ではない。未認証health要求はHTTP403。

監視設定はログ条件5件＋監視自身欠落1件、全6件を無効・通知先空でGitに保存する。
外部登録・有効化・通知到達は未実施。テンプレート保存を監視完了とは扱わない。

## レビューの採否

- Gemini Pro最終版はBLOCKER/WARN/INFO各0件。Claude Opus 5高は最終BLOCKER 0 / WARN 5 / INFO 7。IAM必須権限の誤指摘はレビュアーが撤回。
- 両社への最終送信本文は空白除外11,119文字・hash3659551272で一致確認後に送信。
- 独立SEC/QA/品質はBLOCKER/WARN各0件。QA68件成功。品質INFOの再配備表現は修正。
- 採用: 時間予算、合計件数上限、時計差60秒、安全なHTTP分類、監視自身の欠落条件、
  初回指標到達と停止/復旧試験の順序、非root、試験後の監視停止手順。
- 不採用: view限定閲覧にプロジェクト全体のlogEntries.listを追加する案。
  公式entries.listは指定resourceNamesでlogEntries.list/privateLogEntries.list/views.accessの
  いずれかを要求する。実機で許可/拒否を確かめ、失敗しても権限を自動拡張しない。
- 不採用: progressの読み飛ばし。検索自体がstarted/finishedに限定されており、
  対象外応答は照合失敗にする契約を維持する。
- 不採用: 本番_Default sinkの除外変更。検証のservice_name限定を維持し、本番設定は変更しない。
- 限界: 通信中の厳密な強制停止、依存完全固定、実通知、実Gmail/PubSub照合は未検証/未対応。

根拠: [Google公式entries.listの権限](https://docs.cloud.google.com/logging/docs/reference/v2/rest/v2/entries/list)、
[Google公式の指標欠落条件](https://docs.cloud.google.com/monitoring/alerts/metric-absence)。

Claude最終WARNの判断：

1. 合成未終了の反復通知：試験後は専用Scheduler停止と検証アラート無効化を必須とする。
   停止前は検索期間内の同じ実行を繰り返し検出し得る。通知回数は実測していないため断定しない。
2. 0件監視の平常時通知：初回プローブの到達確認後だけ有効化し、試験後に無効化する。
3. 全viewへの誤付与：プロジェクトレベルの条件付き付与ではなく、専用viewリソースへの直接付与を
   予定している。付与後policyを再読取し、対象外アクセス拒否を確認する。まだ未付与。
4. 通知先空の有効化：登録前に通知先が1件以上・本人指定先であることを確認し、有効化後に
   受信を実測する。現時点の全6テンプレートは無効のまま。
5. invalid分類の粒度：stageと固定error_kindで初期切り分けする。件数上限/ページ上限/不正認証応答の
   固定個別理由コード追加は見送り。検証専用の診断粒度の限界として残す。
