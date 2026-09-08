# Gmail通知期限更新の配備前実サービス検証

2026-09-08 主機CX。基準コード `40fd685`。本イシューは未完了。
本番配備・本番run・業務Gmailの通知設定変更は行っていない。

## 実測結果

| 対象 | 結果 |
|---|---|
| ローカルPostgres | Homebrewで17.11を導入。使い捨てクラスタをUnixソケットのみで起動、独立QA後に停止 |
| 隔離スイート | 19 passed / 1 deselected / 1 warning、0.83秒。19件中18件が実DB fixtureを使用、1件はDB不要 |
| Google通信・復号 | 本人同意を保存済み。実Gmailのwatch更新2回・スキップ1回、実復号を確認 |
| Pub/Sub | 検証topicとpull subscriptionを作成・読み取り照合済み。Gmailの発行権限を検証topicだけに付与・照合済み |
| Cloud Logging | 専用bucket・sink・非公開Cloud Run作成済み。起動ログ2件の専用bucket到達・検索を確認。合成製品ログ11行の到達・検索・集計一致を独立確認 |
| 監視 | 監視6条件を登録。一部失敗・HTTP500・終了欠落・監視エラー・到達未確認の5種類7通を受信確認。停止/復旧通知は試験中 |
| Google連携設定 | 外部Testingアプリ・デスクトップclient・指定Gmail1件を設定済み。本人同意済み |

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
Gmail由来の通知1件を取得し、指定アカウントと隔離DBのhistoryId一致を確認。本人の指定した検証GmailはVaultに記録し、コードへ埋め込まない。

## 権限と認証の状況

サポート連絡先登録と検証topicへのGmail発行権限は、本人の明示承認後に設定・再読取済み。
アプリ名は「CRM Gmail 隔離検証」、外部Testing、指定Gmail1件だけがテスト利用者。
有効なデスクトップclientは `crm-gmail-watch-trial-desktop-v2`。
秘密値はmacOSキーチェーンに格納し、ダウンロードJSONは削除済み。
最初の未使用clientはブラウザDOMのコピー用ラベルに秘密値が含まれ、ツール出力へ
露出したため直ちに削除した。削除後に一覧0件を確認してからv2を作成した。
露出したclientは使わない。v2の秘密値は出力していない。

本人のGoogle同意を保存済み。専用のローカル受け取り口はPKCE・state・1回限りcallbackを
使い、gmail.metadataだけを要求する。profileで指定Gmail一致を確認してからrefresh tokenを
キーチェーンへ保存する。指定検証Gmailで初回登録・再更新の2回が成功した。業務Gmailは変更していない。

自動承認審査の拒否後、本人が「検証用の権限3点とテスト通知を承認」と明示回答。次を設定・独立再読取済み。

- 検証bucketの `_AllLogs` viewだけに `roles/logging.viewAccessor`。
- 同名検証Cloud Runだけに `roles/run.invoker`。
- 本人から検証SAのID tokenだけを発行する `roles/iam.serviceAccountOpenIdTokenCreator`。
- 本人宛ての監視テスト通知。通知チャネル1件と監視6条件を登録済み。

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
- 現在のrevisionは `crm-gmail-watch-trial-00006-6vl`。同じ修正版イメージで異常設定試験後に300/86400秒へ復旧。
- `/api/healthz`、合成4ケース、全ページ取得後の開始/終了突合を用意。
- 検索40秒予算は前後チェック。通信中の厳密な強制停止は保証しない。
- 独立QAは新規37件＋既存31件＝68件成功。実クラウドの到達検証とは別。
- アラートは業務失敗・HTTP500・終了欠落・監視取得失敗に加え、監視自身の
  `check_completed`欠落も必要。今回の登録前はMonitoringポリシー0件、登録後6件を実測。

## 次の手順

1. 監視そのものの停止通知と、再開後の復旧通知を実測する。
2. 個別承認後、検証bucket内の許可view/権限未付与の空viewへのアクセス200/403を確認する。
   Google同意・検証IAM3点・本人宛通知の承認と設定は完了済みで、再確認不要。
3. 専用Scheduler停止・検証アラート無効化・実測記録の保存で区切る。
   実Gmail/実PostgresとPub/Sub、合成製品ログ到達は成功済み。再runしない。
   本番配備・本番runは別途指示後。

## 2026-09-08の検証サービス反映

修正版5ファイルだけを送信し、Cloud Build内の `import app` に成功。
非root（65532）指定のrevisionがReadyになり、起動時TCP確認も1回で成功。
起動ログ2件を専用bucketの `_AllLogs` viewから検索できた。実行者は既存gcloud利用者であり、
検証SAのview限定権限が通る証拠ではない。未認証health要求はHTTP403。

監視設定はログ条件5件＋監視自身欠落1件、全6件を無効・通知先空でGitに保存する。
実機では本人指定channelを設定して有効化し、通知受信を試験した。Gitのテンプレートは無効のまま。

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
- 限界: 通信中の厳密な強制停止・依存完全固定は未検証/未対応。実通知と実Gmail/PubSub照合の進展は後述。

根拠: [Google公式entries.listの権限](https://docs.cloud.google.com/logging/docs/reference/v2/rest/v2/entries/list)、
[Google公式の指標欠落条件](https://docs.cloud.google.com/monitoring/alerts/metric-absence)。

Claude最終WARNの判断：

1. 合成未終了の反復通知：試験後は専用Scheduler停止と検証アラート無効化を必須とする。
   停止前は検索期間内の同じ実行を繰り返し検出し得る。24時間継続時の通知総数は未実測。今回の受信件数は後述。
2. 0件監視の平常時通知：初回プローブの到達確認後だけ有効化し、試験後に無効化する。
3. 全viewへの誤付与：プロジェクトレベルの条件付き付与ではなく、専用viewリソースへの直接付与を
   実施した。付与後policyを再読取済み。対象外アクセス拒否の実測は後述の保留。
4. 通知先空の有効化：登録前に通知先が1件以上・本人指定先であることを確認し、有効化後に
   受信を実測する。現時点の全6テンプレートは無効のまま。
5. invalid分類の粒度：stageと固定error_kindで初期切り分けする。件数上限/ページ上限/不正認証応答の
   固定個別理由コード追加は見送り。検証専用の診断粒度の限界として残す。

## 実Gmail・実Postgresの照合（本人同意後）

独立QAがレビュー済み一時スクリプトを1回だけ実行。製品cron route・Google通信・
キーチェーン由来の検証OAuth・本物の暗号化/復号・psycopg接続を使用した。
接続先はUnixソケット専用クラスタの新規DB、対象は本人指定の検証Gmail1件。
本番の鍵や旧 `.env.local` は使用していない。

| ケース | HTTP | 判定 | 更新件数 | ログ行数 | 秒 |
|---|---:|---|---:|---:|---:|
| 初回登録 | 200 | success | 1 | 5 | 0.663 |
| 期限に余裕あり | 200 | skipped | 0 | 4 | 0.013 |
| 既存登録の更新 | 200 | success | 1 | 5 | 0.630 |

3ケース・ログ14行・実更新2回。全ケース失敗0、DB対象行1、HTTPと終了ログの集計一致、
run_id統一・連番・既存historyId維持を確認。秘密値/メールアドレスがHTTP・結果ログへ
混入していない検査も成功。`/private/tmp/crm-gmail-live-trial-result.json` のcomplete=true。
一度きり実行印は保持し、本文取得目的の再runはしない。

限界：最小7列のPrisma相当テーブルで、全スキーマやNeonは未検証。ローカルFastAPI
TestClient経由であり、Cloud Runで実Gmail処理を実行した証拠ではない。
実Gmailの異常応答は人工注入していない。Pub/Sub到達は別に照合する。

### Pub/Subの実測と残る限界

独立QAが検証subscriptionだけから1回pullし、通知1件・重複0を確認。
通知アカウントと指定Gmailの一致、通知historyIdと隔離DB保存値の一致を確認し、
個人情報を含まない集計を保存してからACKを送った。ACK成功応答も確認。
`/private/tmp/crm-gmail-pubsub-trial-result.json` はcomplete=true・ack_status=confirmed。
実watch更新2回に対して取得通知1件。更新ごとの1対1到達は未確認で、原因を推測しない。
製品Webhookへのpushは設定しておらず、Webhook受信処理は未検証。
使い捨てPostgresは照合後に停止、データと実行印は保持した。
検証Gmailのwatchは今回の期限まで残る。自動更新Schedulerは作成していない。

Google本人同意・検証用IAM3点・本人宛通知の承認は完了済み。再同意を求めない。
本番配備・本番試運転は実施していない。

## 検証IAMと監視の実設定

本人の明示承認後に設定し、独立SECがAPIで再読取。3権限・6監視条件・通知先の一致を確認。

| 設定 | 内容 |
|---|---|
| 検証view | `_AllLogs` 直接のviewAccessor、検証SAのみ |
| 検証サービス | run.invoker、検証SAのみ |
| 検証SA | IDTokenCreator、本人のみ |
| 通知channel | `16878267359483892241`、本人指定メール、enabled |
| 専用Scheduler | `crm-gmail-watch-trial-check`、毎分GET/check、OIDC、再試行0 |

CLIの代理ID token発行は未承認のgetAccessTokenを要求して失敗した。
権限は追加せず、本人tokenからgenerateIdToken APIを直接呼び成功した。
独立QAは17:51〜17:59 JSTのScheduler由来9要求すべてHTTP200を確認。

### 合成ケースと通知の実測

| ケース | 実応答/ログ | メール受信（JST） |
|---|---|---|
| 一部失敗 | HTTP200、renewed1/failed1 | 18:08に受信確認 |
| HTTP失敗 | 成功集計後HTTP500、別途設定エラーのHTTP500も発生 | 18:04に受信確認 |
| 終了欠落 | 開始・進捗のみ。猶予後missing_count1 | 18:01、18:06、18:12の3通を受信確認 |
| 監視エラー | 検証GRACE_SECONDSを一時invalid、/checkがconfiguration/500 | 18:04に受信確認 |
| 到達未確認 | 検証検索窓を一時2秒、entry_count0/HTTP200 | 18:11に受信確認 |
| 監視自体の停止 | 成功後に専用Scheduler停止 | 停止・復旧通知を試験中 |

初回4ケースの3+3+3+2=11行の連番・HTTP終了集計を独立QA確認。
通知経路の稼働確認後に新しい一部失敗合成試験を1回追加した。実Gmailは再runしていない。
初回の一部失敗/HTTP500では発報を確認できなかった。反映待ち、通知タイミング明示、
再適用のどれが影響したかは分離していないため、原因を断定しない。
ログ条件はOPENED、停止条件はOPENED/CLOSEDを明示するようテンプレートへ反映。

異常設定試験は検証サービスだけで実施。invalid→300、1/2秒→300/86400秒に復旧済み。
本番サービス・本番cron・本番鍵は変更していない。
同じ未終了IDがrevisionをまたいで通知されたため、永続的な重複排除がない限界も実測された。

### 閲覧拒否試験の保留

本番_Defaultを対象にする案は自動承認審査が拒否し、実行していない。
より安全な方法として、同じ検証bucket内に固定の空view `crm-trial-no-access` を作成。
そこには検証SAの権限を付けていない。検証イメージと検証SAで許可view200/空view403だけを
確認する一回限りのJobスクリプトは独立SEC B0W0。応答本文は読まない。
このJob作成・実行も個別承認が必要として拒否され、本人への確認を表示中。Job未作成。
検証viewの成功と権限設定の再読取は確認済み、拒否の実測・継承権限の網羅確認は未実施。

通知受信の中間集計：一部失敗1・HTTP失敗1・終了欠落3・監視エラー1・到達未確認1、
合計5種類7通。Gmailの本人アカウント内で検証名に限定して全フォルダ検索し確認した。
停止/復旧通知はこの7通に含めない。
プロジェクトIAMも読取確認し、検証SAまたはallUsers/allAuthenticatedUsersへの直接付与は0件。
組織/フォルダの継承権限全体や拒否実測の代わりではない。
