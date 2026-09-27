# Webhook受信監視：実測と実装前の相談案

2026-09-27 主機CX。以下は18:17時点の実装前調査の記録。後続で本人が本プロジェクトの自律実装・反映を承認し、1イシュー制限も解除した。実装・本番API・Slack通知と主機毎時監視は実施済み。最新状態は `webhook_health_operations.md` を参照。GAS認証追加は個別承認が必要なため未実施。

## 結論

`probe_webhook_receipts()`だけでは足りない。購読の異常、変更後の未着、変更なし／判定不能を分ける。
Vercel自体が停止した事故があるため、同じVercelのcronだけに監視・通知を置かない。GASの時計から読み取り診断APIを呼び、GASから本人のSlack DMへ通知する構成を提案する。

## 実測

git fetch成功、mainはorigin/mainより1件先行。既存の未pushは `b32a84e`（kintone再送・回収スクリプトとテスト）。pushは自動本番反映のため実施しない。
本番Postgresはread_only=Trueのトランザクション・15秒のSQL期限でSELECT。認証情報は既存config/.envを利用、値は出力せず。config/.env・dashboard/.env.localともgitignore対象。

2026-09-27 18:17:54 JSTのWebhookReceipt：

|送信元|最終受信（JST）|経過|累計|
|---|---|---:|---:|
|Notion|9/27 18:02:59|0.25時間|21,334|
|Zoho|9/27 18:01:57|0.27時間|333|
|kintone|9/27 16:33:17|1.74時間|370|
|シート|記録なし|算出不能|記録なし|

シートはテーブルに行がない。編集なし、GAS未登録、送信失敗、記録失敗の区別は未確定。「故障」と断定しない。
DBの日時列はUTCを保存したtimestamp（タイムゾーンなし）。JSTへ明示変換した。初回調査SQLはJSTセッションで差を計算し9時間ずれたため破棄し、UTCセッションで再計測した。既存probeはUTCとして解釈しており、この調査SQLの問題はない。
最初の接続options指定ではOperationalError。接続後read_onlyとSET LOCALを用いた接続は成功。設定ファイルの変更なし。

Notion：9/18〜9/26のJST暦日で、AuditLogのNotion書き込み1,012件、WebhookEventのNotion受信0件。9/27は18:17時点で書き込み330件・イベント211件。累計受信とイベント数は再送等で一致しない。日次件数の1対1比較はしない。
現在は最終受信より後のAuditLog書き込み0件。6DBの最新編集1件ずつを公式POST /databases/{id}/query（検索専用）で取得し全6件HTTP200：取引先9/27 16:33、チェーン8/10 15:36、連絡先9/27 17:12、案件9/27 18:00、商品8/15 01:00、アクション9/27 14:54（JST）。今回の最新編集は最終受信より前。

Zoho：GET /crm/v3/actions/watchがHTTP200、6エントリー・more_records=false。既存channel_id 1786495650520にProducts/Contacts/CustomModule2/CustomModule3/Deals/Accounts、全てcreate/edit/delete。全件の期限は9/28 12:00:05 JST、全件の通知先は本番の/api/webhooks/zohoと一致。watchのPOST/PUTは実行していない。認証用OAuth更新のみ既存クライアントにより実施。

kintone：3アプリの更新日時だけを各1件GETし全てHTTP200。最新更新は取引先9/27 16:33 JST、案件2023/11/2 19:01 JST、アクション2026/8/14 13:04 JST。利用頻度が大きく違うため、アプリ共通の無受信時間では異常判定できない。

## 判定材料と限界

|送信元|購読・設定の直接確認|追加で使う材料|判定の限界|
|---|---|---|---|
|Zoho|GET watchで期限・対象・通知先を取得可能（本番実測済み）|受信記録|購読が有効でも配送・同期成功の保証ではない|
|Notion|公開API一覧・Webhook仕様に購読状態取得APIを見つけられず。公式は設定画面確認を案内|AuditLog＋6DBの最新編集時刻＋受信記録|pausedそのものは断定できない。部分的な欠落は全体最終受信だけでは見逃す|
|kintone|期限更新方式ではない。調査した公開API一覧にWebhook設定取得APIを見つけられず|3アプリの最新更新＋受信記録|CSV・複数レコードAPI等はWebhook対象外。更新だけでも停止と断定できない|
|シート|GAS編集トリガー。期限つき購読ではない|GAS側の登録・実行・送信失敗記録が必要|本番GASの登録者・トリガー・認可状態は今回未検証。Sheets APIの疎通では証明できない|

- `src/diagnostics/integrations.py:496`：無受信だけでは誤報になるというdocstringの判断を維持する。
- `src/sync_engine/webhook_receipts.py`：送信元ごとの最終時刻と累計のみ。日次分布・モジュール別履歴はない。
- `src/api/routes/webhooks.py:95`：受信記録はハンドラ終了後、401以外を記録。同期成功件数ではなく、記録自体もbest effort。例外・記録失敗で未着に見える可能性がある。
- `src/sync_engine/zoho_watch_channel.py`：watch_channel_existsは照会失敗もTrueにする「再登録を安全に抑える」関数。監視でTrue=正常と流用しない。
- `gas/onEdit.js:92`：URL/secret未設定はconsole.error＋return、HTTP失敗はmuteHttpExceptions＋console.error。HTTP失敗が例外にならず、標準の失敗メールに頼れない場合がある。
- GASのAPI書き込みでは編集トリガーは発火しない。シートへ大量に同期したことは、シート発Webhookが届くはずという根拠にならない。
- `vercel.json`：daily-batchは日次、Zoho renewalは6時間おき。`cron.py`のrenewalは失敗をHTTPに返すがSlack通知なし。daily-batchは帳票処理が先で、Vaultには掃除未稼働の未調査TODOもあり、そこだけへの追加は勧めない。

## 本人に相談する実装案（未承認）

GAS毎時 → 認証付きの専用読み取り診断API → 判定 → GASからSlack本人DM。
業務レコードを書き換えずに観測する。自動修復は追加せず既存Zoho延長に任せる。GASに通知状態を保持し、重複を抑える。既存のDB/APIクライアントはPythonで再利用、判定は外部接続なしで単体検証できる形に分ける。

初期閾値は実測から確定できるほど履歴がないため、以下は相談用の仮値：

1. **異常**：Zohoの購読消滅・期限切れ・必要な対象/送信先の不一致。確認できた時点で通知。残り6時間以下は「期限注意」。API失敗は「監視不能」と区別し、2回連続で通知。
2. **未着の疑い**：Notionに最終受信後の実変更があり、その変更から2時間以上経過、次の毎時確認でも受信が進まなければ通知。通常は変更から約3〜4時間以内。AuditLogを主材料、最新編集APIを補助にする。kintoneは同条件でも非通知操作があるので「要確認」にとどめる。
3. **変更なし／判定材料なし**：受信が古いだけで異常にしない。全4経路の状態を1日1回の監視実行報告に掲載。シート受信記録なしは「未確認」と明記し続ける。初版でシート経路の健全性を保証したとは言わない。
4. 同じ異常の再通知は24時間ごと、復旧時に1回。Slackが成功応答したときだけ通知済みを確定。診断API到達不能もGASから通知するためVercel全体停止を拾える。
5. 本番GASの所在・登録者・トリガーを読み取り確認した上で、編集トリガー自身の送信成否記録と定期状態報告を追加するか決める。定期報告だけでは実際の編集イベント発火まで証明できない。変更が全くない期間にも確実な生死保証が必要なら、業務同期から除外した専用テスト領域での往復確認を別途設計・承認する。
6. GASそのものの停止は自分では通知できない。日次の成功報告欠落で気づける形を初版の最低線とし、無人での完全監視には別基盤からの監視成功時刻チェックが必要。この限界を承認時に明示する。

見送る案：全ツール一律N時間で異常（誤報）、daily-batchだけに相乗り（Vercel停止と共倒れ）、業務レコードに試験更新（自動処理・逆流の危険）、購読自動再開の追加（今回の監視とは別の書き込み）。

承認後：実装→動物3体並列レビュー→Gemini/Claudeレビュー→修正・隔離検証→本番反映前に別途指示待ち。現時点では実装もレビュー依頼もしていない。イシュー2・3へは進まない。

## 公式資料

- Notion Webhooks（状態確認はWebhooksタブ）：https://developers.notion.com/reference/webhooks
- Notion公開API一覧：https://developers.notion.com/llms.txt
- Zoho Get Notification Details（公式v8、現行実装v3は本番GETで別途確認）：https://www.zoho.com/crm/developer/docs/api/v8/notifications/get-details.html
- kintone Webhook設定（非通知操作、有効化、毎分上限）：https://jp.kintone.help/k/ja/app/set_webhook/webhook
- GASトリガー（API書き込みは発火せず、作成者の権限で動く）：https://developers.google.com/apps-script/guides/triggers/installable
