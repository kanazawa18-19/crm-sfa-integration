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
| `reason` | iteration_completed / no_connections / not_due / not_configured / execution_failed / log_write_failed / interrupted。進捗中はnull |
| `completed` | 全対象の走査が終了したか。**trueでも一部・全件失敗はあり得る** |
| `counts.total` | 取得した担当数。取得前・取得失敗はnullで、0と区別する |
| `counts.attempted` | 更新処理を試行した数。期限判定自体が失敗した担当も含む（その場合Googleには送らない） |
| `counts.renewed` | Google応答の解釈とDB更新1件・commitが正常終了した数 |
| `counts.failed` | 更新処理が例外を返した数。**外部で何も変わっていないという意味ではない** |
| `counts.skipped`, `counts.skip_reasons.not_due` | 期限に余裕があり更新を省いた数。現在、担当別スキップ理由はnot_dueのみ。理由を増やす際は集計形式も変更する |
| `counts.in_flight` | attempted − renewed − failed。中断時に結果が確定していない数 |
| `counts.remaining` | total − attempted − skipped。未着手数。対象数不明ならnull |

全件走査後、失敗0・更新1件以上ならsuccess。成功した更新と失敗が混在ならpartial_failure。
更新成功0で失敗ありはfailed（期限スキップが混じっても同じ）。更新不要だけならskipped。
対象0件はno_connections、全件期限スキップはnot_dueで区別する。
全体例外でも更新済みがあればpartial_failure、なければfailedとし、completed=falseを付ける。
期限判定で1名の値が不正でも、その担当を失敗に数えて後続担当を継続する。

個人のメール・氏名・本文・鍵・Google応答・DB値・例外文字列・topic名・historyIdは記録しない。
cron以外の製品コードから `renew_all_watches()` を呼ぶ箇所はsrc/scripts検索で0件。
内部辞書は既存テストとの互換のため維持する。新たな呼出元が作られる場合も
その辞書をログやHTTPへ流さず、集計を使う必要がある。
個別失敗は固定のrenewal_failedに集約するため、このログだけでは復号・Google・DBの
どの段階が原因かは特定できない。段階別の分類追加は今回見送り、結果確認に必要な項目へ限定した。
DB保存対象が途中で消えた場合は更新0件を検出して失敗にする。
Googleが変更済みでも、その後のDB例外や通信中断ではfailed/in_flightになり得る。
`renewed`も通知が実際に届くことの証明ではない。配備後の隔離Gmail/Pub/Subで別途照合する。

## HTTPと中断の扱い

- 通常走査は従来どおりHTTP 200。本文はfinishedと同じ集計。**HTTP 200だけで成功判断しない。**
- 設定不足・一覧取得などの全体例外はHTTP 500。detailには安全な終了集計だけを入れる。
- 結果ログ出力の例外は固定の専用例外に変換し、元の業務例外や出力先例外の本文を連鎖させない。
  開始/進捗の出力失敗は業務処理を止め、終了集計を1回記録しようとする。
  終了行も出せない場合はHTTP 500のdetailに `log_write_failed: true` と安全な終了集計を返す。
  この場合のHTTP本文はログ到達の証拠ではない。業務完了後の終了ログだけが失敗すれば
  status=success・completed=trueとHTTP 500が並ぶが、業務は完了済みなので再runしない。
  一部行の出力後にflushが失敗した場合もあり、ログ有無だけで断定しない。
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

### 配備前に決める監視（今回未設定）

HTTP状態だけの監視では担当別失敗を見逃すため、配備イシューで次を設定・試験する。

```text
jsonPayload.job="gmail-watch-renewal"
jsonPayload.event="finished"
(jsonPayload.status="failed" OR jsonPayload.status="partial_failure"
 OR jsonPayload.status="interrupted" OR jsonPayload.completed=false)
```

これはログ上の要調査候補を絞る条件で、アラート自体は未設定。
`started` があるのに `finished` がない実行は、run_idで開始/終了を突き合わせる別の
定期チェックが必要になる。単一行の検索では検出できない。待つ時間の基準は
配備先の最大実行時間とログ取り込み遅延を確認して決定し、稼働中を異常と呼ばない。
開始行から出力できない場合に備え、Cloud RunのHTTP 500とScheduler到達も監視する。
skippedは失敗扱いせず、no_connections / not_due のreasonを分けて読む。
この監視とログ取得の実測が済むまでは、本番試運転へ進まない。

完走時のログ行数は `3 + 2×更新試行担当数 + 期限スキップ数 + 判定失敗担当数`。
（started・対象取得後progress・finishedの3行を含む。中断・ログ失敗時はこの式どおりではない。）
各行は同期flushするため、実データ規模での所要時間は配備前に測る。現時点では未測定。

## 隔離検証

`tests/gmail_sync/test_watch_result_isolated.py` は外部ソケット接続を禁止し、
DB接続だけを使い捨てSQLiteへ差し替える。製品のSQL更新、Gmailの応答解釈、cron認証、
集計、HTTP応答を動かす。Google通信と復号は偽物で、本番環境変数や鍵を読み込まない。
成功/一部失敗のSQL保存件数とGoogle要求数を集計と照合し、初回historyId保存と
既存historyId維持も確認する。例外に架空のメール・秘密相当の目印を混ぜ、ログと応答の
両方に含まれないことを検査する。ログ捕捉はDEBUGまで有効にする。
出力失敗を開始時・部分保存後・全体例外の処理時・正常完了時に注入し、
元例外がトレースバックへ連鎖しないことと、安全なHTTP500/実保存件数を確認する。

協調的中断は1件保存後にKeyboardInterruptを発生させ、保存1件・実行中1件・未着手1件を照合。
強制中断は別プロセスを実際にkillし、開始/進捗がファイルへ到達し終了行がないことを確認する。
読み取り途中の断片はJSONと解釈せず、改行まで書き終わった行だけを照合する。
強制中断の個別更新は偽物で、DB永続化検証は協調的中断のケースが担当する。

**SQLiteの成功は実Postgresの検証ではなく、偽Google応答はGmail/Pub/Subの動作確認ではない。**
今回確認できるのはログの生成・分類と、隔離した保存結果との整合。実サービスとの照合、
本番配備、Cloud Logging検索の実測、本番試運転は本人指示後の別イシュー。

## レビュー状況（2026-09-08）

- 初版 `b5b86c4` は独立SEC/品質/QAでBLOCKER 0。CI全2,700件成功。
- 自動承認レビューがコード貼付を拒否したため、本人が用意した本文を両社へ手動送信した。
  GeminiはPro、ClaudeはOpus 5（高）の表示を確認。Geminiは表示上の省略プレビューを除く
  本文が空白除外21,310文字・ハッシュ93843753で準備本文と一致。Claudeは添付691行を
  実クリックで全文コピーし、同じ文字数・ハッシュの一致を確認した。
- [Geminiレビュー](https://gemini.google.com/app/d3f56cc3201f021a?hl=ja)：BLOCKERなし、条件付きWARN 1件。
- [Claudeレビュー](https://claude.ai/chat/7a0770fe-4b63-4dbb-9c60-30455d4b48f1)：BLOCKER 2 / WARN 7 / INFO 6。
  回答の重要度をそのまま受け入れず、コードと実測で採否を判断した。

| 指摘 | 採否と根拠 |
|---|---|
| Claude B1：更新済みがある全体例外もfailed固定 | 採用。更新済みがあればpartial_failure、completed=false。SQL保存済み件数とHTTP/ログをテストで照合 |
| Claude B2：ログ出力失敗が元例外を連鎖表示 | 採用。固定の専用例外＋from Noneに変換。全体例外の処理を抜けてから終了ログを作り、その失敗も安全なHTTPへ変換。単純に握りつぶして業務を続ける案は、記録なしで本番更新が進むため見送り |
| Claude W1：期限判定失敗で全体停止 | 採用。担当別失敗として数え、後続を継続。Google送信0件の担当と後続の実保存を隔離検証 |
| Claude W2：200だけの監視では検知不可 | 採用。上記に失敗候補の検索条件・開始/終了突合・HTTP500監視を配備前の別作業として明記。設定済みとは扱わない |
| Claude W3：内部辞書にメールキー残存 | 呼出元を検査。製品コードの呼出元はこのcronだけで、応答は安全な集計。内部互換を維持し、新規呼出元での露出禁止を明記 |
| Claude W4/W5：DEBUG捕捉不足・強制終了テストの部分行読み | 採用。DEBUGまで捕捉し、改行済みの完全な行だけを照合 |
| Gemini WARN / Claude W6：rollback未保証の可能性 | 変更不要。各更新はwith _connect()内、接続は毎回psycopg.connectで新規作成。導入済みpsycopg 3.3.4のConnection.__exit__が例外時rollbackし、接続を閉じることを独立SECが読み取り確認。実Postgres試験をしたという意味ではない |
| Claude W7：スキップ理由の将来拡張 | 現状の理由はnot_dueのみと明記。存在しない将来理由のための構造追加は見送り |
| Claude INFO：時刻の二重取得・import順 | 採用。記録時刻を1回取得し終了時刻と共用、import順を整理 |
| Claude INFO：ログ行数・HTTP本文互換 | 行数式を実装から算出し記載。src/scriptsに本文を消費する別呼出元は見つからず、Schedulerスクリプトも業務本文は解析しない |
| Claude INFO：排他なしの実害はScheduler再試行に限定 | この断定は不採用。Vercel併走・手動実行などでも競合し得る。排他なしの制約と再run禁止は維持 |

修正後は独立SEC/品質/QAでBLOCKER 0 / WARN 0、全2,707テスト成功（15.79秒）。
隔離20件＋既存watch11件も成功。依存ライブラリの非推奨警告1件は残る。
本イシューの実装・隔離検証・レビューは完了。配備前の実サービス確認は別イシューとする。
両社への修正版再送は未実施で、修正版の両社再承認を取得したという意味ではない。
本番配備、本番run、鍵再探索は行っていない。
