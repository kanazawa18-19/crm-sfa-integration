# Gmail通知期限更新の結果ログ

2026-09-08 主機CX。対象は `/api/cron/gmail-watch-renewal` の1本だけ。
**本番未配備・実GCP/実Gmail/実Postgresで未検証。本番runはまだ行わない。**

## 選定理由と範囲

残り8本のうち、担当ごとの更新・期限に余裕がある場合のスキップ・失敗を既存コードで
区別でき、メール本文取得やSlack通知を伴わないため選定した。既存の
`watch_registration.renew_all_watches()` の担当別辞書は内部互換用に残すが、
cronのHTTP応答は個人情報なしの集計へ変更する。例外本文・担当メールを出していた
既存例外ログは削除し、内部の失敗値も固定コードにする。他の7本は今回対象外。

```
認証 → started → 対象数取得 → progress → 各更新 → finished
          └──────────── 同じ run_id ─────────────┘
```

標準出力へ接頭辞なしの1行JSONを都度flushする。通常ロガーのレベルに依存しない。
認証失敗は業務を開始せず、業務結果ログも出さない。run_idはサーバがUUIDで作り、
入力ヘッダーやメールアドレスを転用しない。同時実行は別IDになる。
**このIDは重複防止用の鍵ではない。排他制御は追加していない。**

## 記録項目

| 項目 | 意味 |
|---|---|
| `schema_version`, `job` | 形式の版1、固定ジョブ名 |
| `run_id`, `sequence` | 実行のUUID、実行内で単調増加する記録番号 |
| `event` | started / progress / finished |
| `started_at`, `recorded_at`, `ended_at` | UTC。終了前のended_atはnull |
| `status` | running / success / partial_failure / failed / skipped / interrupted |
| `reason` | iteration_completed / no_connections / not_due / not_configured / execution_failed / interrupted。進捗中はnull |
| `completed` | 全対象の走査が終了したか。**trueでも一部・全件失敗はあり得る** |
| `counts.total` | 取得した担当数。取得前・取得失敗はnullで、0と区別する |
| `counts.attempted` | 更新を開始した数 |
| `counts.renewed` | Google応答の解釈とDB更新1件・commitが正常終了した数 |
| `counts.failed` | 更新処理が例外を返した数。**外部で何も変わっていないという意味ではない** |
| `counts.skipped`, `counts.skip_reasons.not_due` | 期限に余裕があり更新を省いた数 |
| `counts.in_flight` | attempted − renewed − failed。中断時に結果が確定していない数 |
| `counts.remaining` | total − attempted − skipped。未着手数。対象数不明ならnull |

全件走査後、失敗0・更新1件以上ならsuccess。成功した更新と失敗が混在ならpartial_failure。
更新成功0で失敗ありはfailed（期限スキップが混じっても同じ）。更新不要だけならskipped。
対象0件はno_connections、全件期限スキップはnot_dueで区別する。

個人のメール・氏名・本文・鍵・Google応答・DB値・例外文字列・topic名・historyIdは記録しない。
個別失敗は固定のrenewal_failedに集約するため、このログだけでは復号・Google・DBの
どの段階が原因かは特定できない。段階別の分類追加は今回見送り、結果確認に必要な項目へ限定した。
DB保存対象が途中で消えた場合は更新0件を検出して失敗にする。
Googleが変更済みでも、その後のDB例外や通信中断ではfailed/in_flightになり得る。
`renewed`も通知が実際に届くことの証明ではない。配備後の隔離Gmail/Pub/Subで別途照合する。

## HTTPと中断の扱い

- 通常走査は従来どおりHTTP 200。本文はfinishedと同じ集計。**HTTP 200だけで成功判断しない。**
- 設定不足・一覧取得などの全体例外はHTTP 500。detailには同じ安全な終了集計だけを入れる。
- KeyboardInterrupt等の協調的中断はinterruptedの終了行を出して再送出する。
  SIGTERMを捕捉する独自ハンドラは追加しない。HTTP応答は保証しない。
- SIGKILL・電源断・プロセス喪失では終了行を保証できない。開始と進捗だけの実行は
  **実行中または結果不明**として扱い、推測の終了時刻を書かない。直近進捗以降にDBが変わった
  可能性があるため、進捗は最後に確認できた数にすぎない。
- 標準出力の失敗やCloud Loggingへの到達失敗もあり得る。ログなし＝未実行ではない。
  HTTPタイムアウトやSchedulerのpauseも処理を取り消さない。本文取得のために再runしない。

## 配備後に使う読み取り条件（今回未実行）

本番試運転前に、配備したリビジョンにこの変更が入っていることと、検証環境での
JSON取り込みを確認する。時刻とリビジョンを実測値で置換する。

```text
resource.type="cloud_run_revision"
resource.labels.service_name="crm-sfa-backend"
resource.labels.revision_name="<配備したリビジョン>"
jsonPayload.job="gmail-watch-renewal"
timestamp>="<試運転開始より少し前のUTC時刻>"
```

該当行の `jsonPayload.run_id` で絞り、sequence順にstartedからfinishedまでを見る。
finishedのstatus/counts/completedを確認し、同時間帯・同pathのCloud Runリクエストログと
Schedulerのジョブ試行を照合する。複数の実行IDがあれば同一実行と決めつけない。
Cloud LoggingでjsonPayloadとして取得できることは未検証。取り込みを確認するまでは
この検索で0件でも未実行と扱わない。既存の試運転手順のログ検索はHTTP用のままである。

## 隔離検証

`tests/gmail_sync/test_watch_result_isolated.py` は外部ソケット接続を禁止し、
DB接続だけを使い捨てSQLiteへ差し替える。製品のSQL更新、Gmailの応答解釈、cron認証、
集計、HTTP応答を動かす。Google通信と復号は偽物で、本番環境変数や鍵を読み込まない。
成功/一部失敗のSQL保存件数とGoogle要求数を集計と照合し、初回historyId保存と
既存historyId維持も確認する。例外に架空のメール・秘密相当の目印を混ぜ、ログと応答の
両方に含まれないことを検査する。

協調的中断は1件保存後にKeyboardInterruptを発生させ、保存1件・実行中1件・未着手1件を照合。
強制中断は別プロセスを実際にkillし、開始/進捗がファイルへ到達し終了行がないことを確認する。
強制中断の個別更新は偽物で、DB永続化検証は協調的中断のケースが担当する。

**SQLiteの成功は実Postgresの検証ではなく、偽Google応答はGmail/Pub/Subの動作確認ではない。**
今回確認できるのはログの生成・分類と、隔離した保存結果との整合。実サービスとの照合、
本番配備、Cloud Logging検索の実測、本番試運転は本人指示後の別イシュー。

## レビュー状況（2026-09-08）

- 独立SECレビュー：BLOCKER 0 / WARN 0。
- 独立品質レビュー：BLOCKER 0。集計の意味を本文へ追記。原因段階の分類追加は
  見送り理由と調査上の制約を明記した。
- 独立QA：既存を含む2,699件成功。追加のcommit失敗ケースを含む隔離13件も独立QAで成功。WARN解消。
- Gemini Pro / Claude Opus 5（高）：新規画面でモデルを確認。Geminiへの貼付が
  自動承認レビューに「内部コードの機密性と宛先への明示承認を確認できない」と拒否され、
  両社への本文送信は未実施。他社レビュー完了までは本イシューを未完了とする。
- 本番配備、本番run、鍵再探索は行っていない。
