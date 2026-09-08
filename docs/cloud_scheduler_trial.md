# Cloud Schedulerの試運転と書き込み影響

2026-09-08 主機CX。対象は `manage_scheduler_job.sh` が扱う9本。
コードの読み取りで確認した内容であり、本番の設定値・対象件数・排他動作は今回未検証。
本番移行・ジョブ作成・実行・有効化は別イシューとする。

## 停止中ジョブを試す流れ

PAUSEDでのRunJobは2026-09-08の実機で拒否された。停止解除は定時実行も再開するため、
**移行前の仮日程だけ**を対象にする。有効化済みの1本目には使わない。

```
  PAUSED ＋ 仮日程 ＋ 宛先・認証・再試行設定の照合
       │
       ▼
  失敗時の停止処理を登録 → resume → run → pause → PAUSEDを再取得
       │
       ▼
  実行結果を別途確認 → 業務結果の照合 → 別イシューで本番移行
```

- 仮日程は `0 0 29 2 *` / `Etc/UTC`。年指定ではなく4年ごとの2月29日であり、
  「永久に実行されない日程」ではない。仮日程の直前・直後にも操作を拒否する。
- 本来の日程、ENABLED、宛先・OIDC設定不一致、設定取得失敗は実行前に拒否する。
- `run` は非同期の要求受付。終了コード0もHTTP 200も、業務の全件成功を保証しない。
- `pause` は既に始まったHTTP処理や書き込みを取り消さない。再停止したから再実行してよい、とはならない。
- 通信切断で `run` が失敗しても、相手に要求が届いた可能性がある。ログを調べるまで再実行しない。
- EXIT/INT/TERMで再停止を試みるが、強制終了（SIGKILL）・端末停止・認証失効時には保証できない。
  復旧時は対象ジョブの状態を読み、ENABLEDなら同じプロジェクト・場所・名前でpauseし再取得する。
  スクリプトも復旧コマンドを表示する。停止操作は業務結果の復旧を代替しない。
- 自動再試行は0を明示し、試運転時にも確認する。ただし配送の重複を完全には排除できない。
- 定刻前後1時間のガードは作業時間帯の制限。Vercelの遅延・手動実行・Gmail Pushとの競合を防ぐロックではない。
- 同じジョブを複数の端末・担当で同時操作しない。状態照合と変更は一つの原子的な操作ではない。

公式仕様： [pause](https://docs.cloud.google.com/scheduler/docs/reference/rest/v1/projects.locations.jobs/pause)、
[resume](https://docs.cloud.google.com/scheduler/docs/reference/rest/v1/projects.locations.jobs/resume)、
[ジョブと再試行設定](https://docs.cloud.google.com/scheduler/docs/reference/rest/v1/projects.locations.jobs)。

## 残り8本の実行影響

**8本ともcron入口にdry-run（書き込まずに試す機能）はない。**
`--allow-business-writes` は意図しない実行を防ぐ明示フラグで、本人の本番実行許可の代わりではない。
通知設定を空にする、機能を無効にする、対象が0件だと推測する方法は業務検証にならない。
入口は [`src/api/routes/cron.py`](../src/api/routes/cron.py)。

| ジョブ | 書き込み・外部送信 | 二重実行対策と限界 | 隔離した試運転に必要なもの |
|---|---|---|---|
| `daily-batch` | Slack日報、金曜は週報。7日超のWebhookイベント記録削除 | 同日再送抑止・ジョブ排他なし。再実行は再投稿 | 検証データソース・DB・本人だけのSlack宛先 |
| `zoho-webhook-renewal` | 既存watchの通知先URL・トークン・6モジュール・期限をPUT更新 | 同じchannelでも設定差があれば上書き競合。排他なし | 検証Zoho/channel。通知先とモジュール、更新後の状態の照合 |
| `gmail-sync` | EmailLog追加、Notion最終メール日時、担当の最終同期日時、優先度の高いメールのSlack DM、web-engagement webhook | メールIDの一意制約あり、ジョブ排他なし。同時INSERTで担当別結果が`-1`になり得る。DB保存後の外部通知失敗は単純再runで回復しない | 検証メールボックス・DB・Notion・通知先。Push起動の同期との競合も確認 |
| `gmail-watch-renewal` | Gmail watch、DB期限、初回historyId更新 | 残り2日以内/未登録だけ対象。既存historyId維持、排他なし | 検証メールボックス・Pub/Sub topic・DB。更新後期限と通知経路を確認 |
| `incident-digest` | EmailLogの`digestedAt`更新後、Slackへまとめ投稿 | 原子的な対象獲得で重複を抑止するが送信失敗で戻らない。URLなしでも通知済みになる | 検証DB＋隔離Slack。DB件数とSlack到達を両方確認 |
| `project-mirror-reconcile` | ProjectMirror追加/更新、カーソル保存、一巡時の古い行削除、異常通知 | 非pooled DB接続のロックと削除量ガード。1回の200で一巡完了とは限らない | 検証Notion案件DB・Postgres・通知先。件数、カーソル、削除候補の前後比較 |
| `relation-sync-reconcile` | ClientNameIndex追加/更新、カーソル保存、一巡時の古い行削除、異常通知 | 非pooled DB接続のロックと削除量ガード。無書き込みではない | 検証Notion取引先DB・Postgres・通知先。件数、カーソル、削除候補の前後比較 |
| `spreadsheet-outbox-drain` | キュー獲得、Sheets行追加、ID対応表、完了/再試行/諦め状態、30日超の解決済み記録削除、異常通知 | キュー獲得と行単位ロック、作成直前検索。実Postgresでの排他は今回未検証。pending=0でも棚卸し・削除あり | 検証DB・Notion・Sheets・通知先。行数、対応表、キュー状態、`needs_attention`を確認 |

### 根拠となる実装

行番号はこの調査時点。後の変更では関数名を辿る。

| 対象 | 根拠 |
|---|---|
| 日報・週報と削除 | `src/reports/batch.py:358,486,490`、`src/sync_engine/webhook_events.py:95` |
| Zoho watch更新 | `src/sync_engine/zoho_watch_channel.py:284,397` |
| Gmail同期と一意制約 | `src/gmail_sync/sync.py:152,368`、`src/gmail_sync/db.py:148` |
| Gmail watch | `src/gmail_sync/watch_registration.py:38,69,80` |
| 通知済みの先行更新 | `src/incident_detection/notify.py:117`、`src/incident_detection/db.py:48` |
| 案件ミラー | `src/project_mirror/sync.py:323,433,436,464,488`、`src/project_mirror/db.py:47` |
| 取引先名索引 | `src/relation_sync/sync.py:229,298,301,333,357`、`src/relation_sync/db.py:45` |
| Sheets書き込み待ち | `src/sync_engine/spreadsheet_outbox_drain.py:77,365`、`src/sync_engine/spreadsheet_outbox.py:251,463` |

## 次イシューで本番の1本を移す際の受入条件

1. 上表の隔離環境で対象処理を検証する。検証DBでも外部通知先やOAuthが本番なら隔離にならない。
2. 必要環境変数と参照先を揃え、対象件数・書き込み・通知・削除のプレビューを本人へ示す。
   件数は読み取りで実測し、まだ測っていないものを「0件」と扱わない。
3. 本人から対象1本の本番実行許可を得る。Vercel cronと他の起動元が動いていない時間帯を選ぶ。
4. 仮日程で作成・試運転。8本は `run <ジョブ名> --allow-business-writes` が必要。
5. Scheduler終了記録、Cloud Runの対象リビジョン・path・開始時刻・HTTP応答を照合する。
   応答本文の担当別エラー、`completed`、`needs_attention`等と、上表の業務結果を確認する。
   結果本文がログに無い場合、読み直すために同じURLを再実行しない。取得手段を先に用意する。
6. 同じpathだけVercelから停止し、Schedulerを有効化。次回定時の結果まで確認する。
   `token-encryption-healthcheck` は例外としてVercelも維持する。

**今回見送った案**：無条件のresume、全8本の一括run、通知先を抜いただけの本番試験、
ロックがあるという理由だけで二重起動を許可する案。いずれも本番の副作用を抑止できないため。
本体の通知欠落対策・実Postgres排他検証は別課題として記録し、このスクリプト修正には含めない。

### activateの途中で失敗した場合

日程更新が成功してresumeが失敗すると、`PAUSED`＋本来の日程が残る。
仮日程限定の安全検査で再度の`activate`は拒否されるため、何度も同じコマンドを繰り返さない。
スクリプトのエラー案内に従い、実際の状態・日程・宛先を読み直す。
本来の日程が正しく、試運転の業務結果確認済み、旧Vercelの同じcron停止済みであることを確認した上で、
承認済みの移行作業として当該ジョブだけを手動resumeする。定刻付近の復旧には欠落・重複の確認も要る。
`token-encryption-healthcheck` はVercelを残す例外。認証エラーを鍵の不一致や再デプロイの必要性と混同しない。

## この修正の検証状況

- 2026-09-08：偽gcloudで状態遷移・拒否・失敗復旧を29件、既存Webhook/cronを51件、計80件通過。
  シェル構文確認も通過。実Cloud Schedulerでの修正版試運転は未実施。
- シロクマ・おばさん・クマの独立レビューで、復旧案内などの指摘を反映。BLOCKER 0。
- Gemini/ClaudeレビューはChrome連携へ接続できず未実施。必須レビューを終えた状態とは扱わない。
  Chrome接続復旧後、同じイシューで差分をレビューし、指摘があれば修正・再検証する。
