# 同期APIの本番配備条件

2026-09-09 主機CX。対象製品コード `bf7158e`（調査文書追加 `ef45f92`）。①障害日次通知・②古い更新の拒否を含む同期APIの配備前確認。本番配備は条件未充足のため保留。本人指定により通知先は金沢さんのDMで確定。Neonログインも完了し、下記のDB実測まで実施済み。

## 確認結果

| 条件 | 実測・根拠 | 判定 |
|---|---|---|
| 現行API | `dpl_5iwWWdhTbz56vhGZtuvC7gurZT4g`、production/Ready、本番alias保持。コミット `19dc5a8` は前セッションの確認値 | 新配備なし |
| テーブル | RecordSyncWatermark・SpreadsheetOutboxは前セッションで適用済みと確認 | 再適用不要 |
| 通知先 | Vercel本番設定名一覧に `SLACK_WEBHOOK_URL_ALERT` なし | 未充足 |
| DB設定 | Vercelの2項目はSensitive。Neon実測は上限112・管理用予約6、確認時の通常接続2本 | ピーク/アプリ並列数との照合は未完了 |
| 稼働設定 | Vercel APIでHobby、Fluid有効、iad1、標準タイムアウト300秒、elasticConcurrencyEnabled=false | アプリの同時処理数上限は未特定 |
| 定時登録 | 実プロジェクトに9本。incident-digestは `0 4 * * *`、outboxは `0 17 * * *`。disabledAt=null | 登録あり。実行・到達の証明ではない |
| 日次通知の実行記録 | 現行配備に限定し過去24時間・incident-digest文字列・HTTP状態別で検索、集計行なし | 成功/失敗/未実行とも断定不可 |
| 監視 | 連続失敗と受理未完了の自動監視は既存資料上未実装。外部監視の設定は未確認 | 未充足 |

Vercel APIはGETのみ。設定変更・配備・同期手動実行・通知送信・修復は行っていない。
チームの `concurrentBuilds=1` はビルドの並列数であり、同期処理の並列数として使わない。
`elasticConcurrencyEnabled=false` も同時処理数が1という意味には扱わない。

## DB接続情報の承認と取得結果

本人からDB接続情報2項目の限定取得・読み取り確認の承認を取得済み。前セッションの自動承認拒否は承認不足が理由だったが、今回は取得呼び出しを実行できた。
公式の個別取得API `/v1/projects/{project}/env/{id}` は両項目とも成功応答、`type=sensitive`、値なし。秘密情報を保存した一時ファイルは作っていない。

[Vercel公式仕様](https://vercel.com/docs/environment-variables/sensitive-environment-variables)上、Sensitive/Secretは保存後に値を読み出せない。これは今回の自動承認拒否ではない。全環境変数の取得、秘密設定の削除/再登録、値を吐く配備は行わない。

本人「ログインしてるで。neon。」の後、ChromeでNeon管理画面の対象DB・ブランチを確認し、読み取りを実施。既存ローカル `dashboard/.env.local` の2項目は、画面の計算資源ID/DB名と一致すること、通常接続がpooler・専用接続が非poolerであることだけを真偽値で照合した。秘密値の表示・ローカルからのDB接続はしていない。Vercel現行秘密値との一致は依然として取得不能で直接照合していない。

## 2026-09-09のNeon実測

対象：`neon-cyan-branch` / `morning-voice-67655784`、main / `br-raspy-hall-awjghosc`、`neondb`、`ep-billowing-art-awbkegin`。Neon Free・0.25 CU・約1 GB RAM、5分で自動休止。SQL Editorで19:26:52 JSTに11文、19:34:24 JSTに6文を実行。いずれも `BEGIN READ ONLY`、`transaction_read_only=on` を取得、最後はROLLBACK成功。セッション時刻帯GMT。

| 指標 | 実測値 | 解釈 |
|---|---:|---|
| max_connections | 112 | 計算資源の接続上限 |
| superuser_reserved_connections | 6 | 管理者用の予約枠 |
| reserved_connections | 0 | 追加予約枠なし |
| 通常枠の理論上限 | 106 | 112−6。アプリ専用の割当数ではない |
| client backend | active 1・idle 1 | 瞬間値、今回の照会接続を含む |
| idle_in_transaction_session_timeout | 300,000 ms | トランザクション内待機は300秒。Slack通信試験ではない |
| 照会ロールの接続上限 | -1 | ロール独自の制限なし。DB全体上限は残る |
| pg_read_all_stats | true | 照会ロールに全体統計の閲覧権限あり |
| 日次通知の未処理 | 0 | 旧APIによる送信済み扱いを含むため実送達を保証しない |
| 過去14日作成の中優先度 | 4 | UTC日付8/31・9/1・9/3・9/8に各1件。最大1件/日 |
| 上記4件の送信済み扱い | 4 | digestedAtあり。最初9/1 04:27:33.246、最後9/9 04:27:33.242（DB表示GMT） |
| RecordSyncWatermark | 総数0・未完了0・15分超0 | 新API未配備なので、巻き戻り対策の稼働実績ではない |
| SpreadsheetOutbox | 総数0 | 作成待ち・打切りとも0。実同期の到達保証ではない |

Neon MonitoringのPostgres接続数欄には `Some metrics are unavailable` と表示され、ピーク値は取得できなかった。poolerの `Max: 10000` はクライアント側上限で、直接接続の112と混同しない。

旧API `19dc5a8` は、先に `digestedAt` を埋め、通知先がなければ送信せず件数を返す。このため4件の `digestedAt` はSlack送達の根拠にできない。4件全部の欠落を確定したわけでもない。Slack履歴との照合・修復は別指示後とし、今回リセット/再送はしていない。

## 接続枠の照合

必要数は、同時レコード同期/outbox修復＋同時行作成ロック＋短時間の事前検査＋その他の利用分。
Neonの最小計算資源時の上限・予約枠・他用途・ピークを含めて照合する。瞬間的な空きだけでは合格にしない。

下記相当の読み取りSQLを上記日時に実行した。書き込みを拒否するトランザクションで実施し、最後はROLLBACKする。接続情報・実行中SQL本文・メール本文をDBから取得しない。

```sql
BEGIN READ ONLY;
SET LOCAL statement_timeout = '10s';
SET LOCAL lock_timeout = '2s';
SELECT name, setting, unit
FROM pg_settings
WHERE name IN (
  'max_connections', 'reserved_connections',
  'superuser_reserved_connections', 'idle_in_transaction_session_timeout'
);
SELECT backend_type, state, COUNT(*) AS connections
FROM pg_stat_activity
GROUP BY backend_type, state;
ROLLBACK;
```

統計の見える範囲・権限も記録する。通常のSQL接続と直接接続が同じ本番DBを指すか、直接接続が接続プールを経由しないかは別途確認が必要。

## 滞留の読み取りと監視条件

```sql
BEGIN READ ONLY;
SET LOCAL statement_timeout = '10s';
SELECT COUNT(*) AS pending_count, MIN("createdAt") AS oldest_created_at
FROM "EmailLog"
WHERE "incidentPriority" = 'medium' AND "digestedAt" IS NULL;
SELECT "createdAt"::date AS created_date, COUNT(*) AS medium_count
FROM "EmailLog"
WHERE "incidentPriority" = 'medium'
  AND "createdAt" >= CURRENT_TIMESTAMP - INTERVAL '14 days'
GROUP BY "createdAt"::date ORDER BY created_date;
SELECT "dbKey", COUNT(*) AS pending_count
FROM "RecordSyncWatermark"
WHERE ("completedAt" IS NULL OR "acceptedAt" > "completedAt")
  AND "acceptedAt" < CURRENT_TIMESTAMP - INTERVAL '15 minutes'
GROUP BY "dbKey";
SELECT status, COUNT(*) AS record_count, MIN("createdAt") AS oldest_created_at
FROM "SpreadsheetOutbox" GROUP BY status;
ROLLBACK;
```

メールの作成日別件数は分類状態の現在値を数えるもので、過去の日別分類発生数そのものではない。DBセッションのタイムゾーンも記録する。

配備前に実装/設定と通知先を確認する監視条件は次のとおり。現時点では稼働確認済みではない。

- 通知失敗：HTTP失敗と失敗連続回数を記録。Slack自身の障害を同じSlackだけで知らせる構成にしない。
- 実行の途絶：登録の有無に加え、定時実行窓＋所要時間の猶予後に最終成功が更新されているか確認する。対象0件の正常実行も成功記録が必要。
- 容量：1日1回・最大50件に対し、未処理件数/最古日時が増え続けないか確認する。`batch_limit_reached=true`だけで残件ありとは断定しない。
- 受理未完了：暫定15分は通常処理時間/再送間隔との照合前。同じ対象が2回以上残り、処理中でないことを確認して調査する。件数だけの一致では同一対象と判断しない。
- outbox：failedは人の対処が必要。pendingの滞留は `nextAttemptAt` と日次実行窓を含めて判定する。

過去の `digestedAt` や受理未完了を一括リセットしない。同期の再送・修復・Slack試験送信は別指示後。

## 通知先の確定と必要な変更

本人「kanazawaのDM」で通知先は確定。通知先の質問を繰り返さない。現行Incoming Webhookへの設定追加だけで完了とはせず、既存Botを使って金沢さんだけのDMへ送る処理を実装する必要がある。SlackユーザーID/DM IDは実アカウントと照合して設定し、推測で埋めない。

過去ログに、Cloud Run用に `config/.env` の `SLACK_WEBHOOK_URL_ALERT` をSecret Managerへ登録した記録があり、現在のローカル設定でも項目の存在を確認した。URLの値は表示しておらず、通知先・有効性は未確認。このWebhookをVercelへ転用してよいとは判断していない。

読み取り担当による独立探索でも、Webhook依存の製品送信箇所は6つと確認した。

| 対象 | 関数 | 維持する失敗時の扱い |
|---|---|---|
| 日次通知 | `incident_detection/notify.py:run_incident_digest` | Slack受理失敗なら例外・DB確定しない |
| 同期競合 | `sync_engine/slack_notifier.py:WebhookSlackNotifier._post` | 本処理を止めず失敗記録 |
| 案件ミラー警告 | `project_mirror/sync.py:_notify_slack_alert` | 本処理を止めず失敗記録 |
| 関連同期警告 | `relation_sync/sync.py:_notify_slack_alert` | 本処理を止めず失敗記録 |
| 商談承認DMの失敗通知 | `meeting_sync/slack_approval.py:_alert_delivery_failure` | 本処理を止めず失敗記録 |
| 暗号鍵自己診断の失敗 | `api/token_encryption_healthcheck.py:_notify_slack_alert` | 診断結果を維持 |

次の実装イシューでは外部依存の少ない共通DM送信処理を追加し、HTTP成功だけでなくSlack JSONの成功を確認する。秘密や応答本文を失敗ログへ出さない。既存manager_dm/slack_approvalの循環importを増やさない。案件ミラー/関連同期は既存管理者DMも同時に呼ぶため、金沢さんへ重複通知する可能性を整理する。既存の高優先度・管理者宛通知の要件を無断で狭めない。

今回の配備条件確認は、実測と必要変更の特定まで。DM送信処理・同時処理数の上限/容量保証・自動監視が残るため、新APIは配備しない。次はDM通知対応を1イシューとして実装・レビュー・隔離検証し、その後に残る配備条件を順に満たす。

関連：[日次通知](incident_digest_delivery.md)、[更新時刻と接続条件](record_sync_freshness.md)。
