# Webhook受信監視の運用

GAS「CRM-SFA Webhook受信監視」が毎時、読み取り専用の診断APIを呼び、CRM専用Slack botから金沢さん本人のDMへ通知する。Vercelが止まってもGASから通知できる。業務レコード・通知購読は変更しない。

```
GAS（毎時） → 診断API → 受信記録・実変更・Zoho購読
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
- **日次報告が来ない**：GASの実行履歴、トリガー、Slack認証、失敗通知メールを確認。GAS自体・Slack停止をこの監視単独では通知できない。

## 限界

受信はツール全体の集計。一部のNotion DBやkintoneアプリだけ止まり他が動くケース、同期処理の成否、Zoho/シートの変更後未着は保証しない。Notionの監査ログはNotionへの成功した実変更だけを記録する。システム自身の更新も含む。Notion最新ページ照会は1ページ/DBで、削除イベントだけの欠落は検出できない。kintoneは通知されない操作と障害を機械的に区別できない。

## 設置・確認

専用GAS ID: `1HviGFC-BG2LWys26QDxmNYBla-rZcfRIQa_l2_Nk0sbFqS91m2r_rI4d`。コードは `gas/webhook-health/`。認証情報はGASのスクリプトプロパティ（ソースに埋め込まない）。必要キーは `WEBHOOK_HEALTH_TOKEN`、`SLACK_BOT_TOKEN`、`SLACK_DM_CHANNEL`（本人DM）。Vercel productionには同じ監視専用トークンを置く。業務APIの認証とは分離。

1. `previewWebhookHealth`で通知文を確認（投稿・状態保存なし）。
2. `webhookHealthTick`で初回日次報告のSlack受理を確認。
3. `installWebhookHealthMonitor`で毎時トリガー1本を設定。複数なら自動削除せずエラーにする。
4. `webhookHealthStatus`で設定・トリガー数・最終成功を確認。GAS失敗通知メールは即時に設定。

停止時はこの専用プロジェクトの `webhookHealthTick` トリガーだけを削除する。他の同期トリガーや購読は触らない。
