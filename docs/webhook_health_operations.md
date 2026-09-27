# Webhook受信監視の運用

2026-09-27現在、主機のlaunchdが毎時、読み取り専用の診断APIを呼び、CRM専用Slack botから金沢さん本人のDMへ通知する。Vercelが止まっても主機から通知できる。業務レコード・通知購読は変更しない。GAS版はコードと専用プロジェクトを用意した段階で、認証登録・OAuth同意・トリガー設置は未実施。

```
主機（毎時） → 診断API → 受信記録・実変更・Zoho購読
   └───────────→ 本人のSlack DM
```

## 判定と誤報対策

|観測|判定|
|---|---|
|Zoho購読なし・失効・必要な操作不足・通知先不一致|即時通知|
|Zoho期限まで6時間以内|要確認（自動更新は6時間周期、期限は1日延長）|
|Notion/kintoneに実変更があるが受信が進まない|変更から2時間以上、かつ48分以上あけた再確認で要確認|
|API・認証・受信記録など取得不能|48分以上あけた再確認で監視不能|
|変更がなく受信もない|異常と断定しない|
|シート受信記録なし|未確認として日次報告|

同じ警告の再通知は24時間以上後。受信再開・正常確認は1回だけ通知する。毎日JST日付が変わった後の最初の実行で日次報告する。通知がSlackに受理されなければ状態を確定せず次回再送（2通目だけ失敗した場合は1通目が重複することがある）。

## 通知を受けたら

- **Zoho購読**：`scripts/register_zoho_webhook.py` と購読延長cronの実行状況を調べる。監視は自動修復をしない。
- **Notion受信**：Notion integrationsのWebhook購読画面でpaused・配信失敗を確認。公共APIから購読状態を取得できないため、実変更と受信の比較で補う。
- **kintone受信**：対象アプリのWebhook設定・配信履歴を確認。CSV、一括REST操作、通知対象外のステータス変更でも更新日時は進むため停止とは断定しない。
- **シート未確認**：既存の編集トリガーと配送先を確認。APIによるセル変更は編集トリガーを起動しない。`setupAll`を監視のためだけに実行しない。
- **監視不能**：Vercelの稼働、専用トークン、外部API権限・通信を確認。秘密値やHTTP例外の原文はSlackに貼らない。
- **日次報告が来ない**：主機の電源・スリープ・ログイン状態、launchdログ、Slack認証を確認。主機自体・Slack停止をこの監視単独では通知できない。

## 限界

受信はツール全体の集計。一部のNotion DBやkintoneアプリだけ止まり他が動くケース、同期処理の成否、Zoho/シートの変更後未着は保証しない。Notionの監査ログはNotionへの成功した実変更だけを記録する。システム自身の更新も含む。Notion最新ページ照会は1ページ/DBで、削除イベントだけの欠落は検出できない。kintoneは通知されない操作と障害を機械的に区別できない。

## 現在の主機運用

主機 `/Users/cnctor/crm-sfa-integration` のみ設置。サブ機に重ねて設置しない。再起動後はログインするまで実行されず、スリープ・電源断中も止まる。

- `scripts/install_local_sync_monitor.py` は既定で設定を表示し、`--apply` で登録する。秘密値はplistに入れない。
- `jp.cnctor.crm-sfa.health`：毎時。`scripts/run_local_sync_monitor.py health --preview` で無送信確認、`--status` で最終成功を見る。
- `jp.cnctor.crm-sfa.inventory`：月曜06:17（MacのJST設定）。週次の全6DB照合。実行上限165分。詳細は `docs/hub_inventory_and_gaps.md`。
- 認証：監視API・Slackは既存macOSキーチェーン。全件照合は既存 `config/.env` を利用（gitignore・0600確認済み）。同期用トークンは書込権限を持つが、このスクリプトは読み取りだけ。追加のクラウド配布なし。
- 状態：`~/Library/Application Support/crm-sfa-integration/`。Slack受理後に原子的に確定。ログ：`~/Library/Logs/crm-sfa-integration/`。同じ実行失敗のDMは24時間に1回。
- 2026-09-27 19:31 JST、実際のlaunchd起動→キーチェーン取得→診断→本人DM受理→last_success更新を確認。所要8.5秒。手元の直接実行だけを成功根拠にしていない。
- 停止：`launchctl bootout gui/$(id -u)/jp.cnctor.crm-sfa.health`（週次は末尾inventory）。永続停止は対応plistもLaunchAgents外へ退避する。
- GASへ移すときは、先に主機healthを停止する。週次をActionsへ移す場合も主機inventoryを停止し、二重稼働させない。

## GAS版の準備（未稼働）

専用GAS ID: `1HviGFC-BG2LWys26QDxmNYBla-rZcfRIQa_l2_Nk0sbFqS91m2r_rI4d`。コードは `gas/webhook-health/`。認証情報はGASのスクリプトプロパティ（ソースに埋め込まない）。必要キーは `WEBHOOK_HEALTH_TOKEN`、`SLACK_BOT_TOKEN`、`SLACK_DM_CHANNEL`（本人DM）。Vercel productionには同じ監視専用トークンを置く。業務APIの認証とは分離。

1. `previewWebhookHealth`で通知文を確認（投稿・状態保存なし）。
2. `webhookHealthTick`で初回日次報告のSlack受理を確認。
3. `installWebhookHealthMonitor`で毎時トリガー1本を設定。複数なら自動削除せずエラーにする。
4. `webhookHealthStatus`で設定・トリガー数・最終成功を確認。GAS失敗通知メールは即時に設定。

停止時はこの専用プロジェクトの `webhookHealthTick` トリガーだけを削除する。他の同期トリガーや購読は触らない。
