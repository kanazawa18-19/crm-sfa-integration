# 同期APIの本番配備条件

2026-09-09 主機CX。対象コード `bf7158e`。①障害日次通知・②古い更新の拒否を含む同期APIの配備前確認。本番配備は条件未充足のため保留。

## 確認結果

| 条件 | 実測・根拠 | 判定 |
|---|---|---|
| 現行API | `dpl_5iwWWdhTbz56vhGZtuvC7gurZT4g`、production/Ready、本番alias保持。コミット `19dc5a8` は前セッションの確認値 | 新配備なし |
| テーブル | RecordSyncWatermark・SpreadsheetOutboxは前セッションで適用済みと確認 | 再適用不要 |
| 通知先 | Vercel本番設定名一覧に `SLACK_WEBHOOK_URL_ALERT` なし | 未充足 |
| DB設定 | `DATABASE_URL`・`DATABASE_URL_UNPOOLED` は存在、両方Sensitive | 値・接続枠の照合未完了 |
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

ChromeでNeon管理画面を開いたが未ログイン。本人ログイン後にDB管理画面で対象DB・ブランチを確認し、以下の読み取りを進める。既存ローカル `dashboard/.env.local` にDB設定2項目が存在することは確認したが、Vercel現行値との一致は未確認で、接続はしていない。

## 接続枠の照合

必要数は、同時レコード同期/outbox修復＋同時行作成ロック＋短時間の事前検査＋その他の利用分。
Neonの最小計算資源時の上限・予約枠・他用途・ピークを含めて照合する。瞬間的な空きだけでは合格にしない。

実行準備した読み取りSQL（未実行）。書き込みを拒否するトランザクションで実施し、最後はROLLBACKする。接続情報・SQL本文・メール本文をDBから取得しない。

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

## 通知先の残判断

過去ログに、Cloud Run用に `config/.env` の `SLACK_WEBHOOK_URL_ALERT` をSecret Managerへ登録した記録があり、現在のローカル設定でも項目の存在を確認した。URLの値は表示しておらず、通知先・有効性は未確認。このWebhookをVercelへ転用してよいとは判断していない。

既存資料の「金沢さん/マネージャーへのDM」は新規レコード問題等の別経路。日次通知のチャンネルを指定した根拠にはしない。日次本文には連絡先メール・担当メール・件名が含まれるため、本人が閲覧対象に合う通知先を指定する。

関連：[日次通知](incident_digest_delivery.md)、[更新時刻と接続条件](record_sync_freshness.md)。
