# Slack携帯CRMの本番反映と限定実物確認（2026-10-02）

## 実施と確認

| 対象 | 実物で確認した結果 |
|---|---|
| DB | `20261002000000_slack_crm_operation` を本番の直接接続で適用。移行40→41件、`SlackCrmOperation` は23列・操作0件で読戻し。 |
| API | `afd3c15`、Slack URL検証修正 `983da55`、実接続修正 `841a89f` をmainへpush。最新のVercel配備READY、GitHub CI `36984397989` 成功。 |
| Slackアプリ | `sales-crm-sfa` のHomeタブON、Event SubscriptionsのURL検証済み、`app_home_opened` 登録済み。Options Load URLを既存Interactivity URLと同じ `/api/webhooks/slack-interactions` に設定。追加スコープと認証交換なし。 |
| 本人の画面 | Bot API `views.publish` 成功。本人用App Homeに「案件を探す」「アクションを記録」の表示を確認。`app_home_opened` は本番APIに到達して200。 |
| 定期回収 | DB接続オプション修正前は500、修正後の本番 `/api/cron/slack-crm-drain` は200。 |
| ローカル | 修正後のSlack関連テスト18件成功。 |

## 未完了の実物確認

本人用App Homeで2つのボタンを押しても、案件選択画面が開かない。押下後の本番ログに `/api/webhooks/slack-interactions` の到達記録はなかった。Slack公式仕様ではボタン押下時に `block_actions` を送るため、設定またはSlackクライアント側の挙動を引き続き切り分ける。本人宛DMに同じ操作ボタンを含む試験メッセージ1件を送信したが、ブラウザでDMタブへ切り替われず、そのボタンの実押下は未確認。試験DMの削除は自動承認審査で拒否されたため、本人宛に残っている。

`SLACK_CRM_WRITE_ENABLED` は未設定。専用試験レコードでの案件更新、アクション作成・編集、二重送信、結果DM、Notionと同期先の読戻し、携帯実機確認は未実施。一般の顧客レコードには書き込んでいない。Gmailの14日観測・3日並走も未経過で、切替はしていない。Notion項目別同期の実Webhook確認も未完了。

## 続き

1. 本人の携帯SlackでHomeのボタン押下を確認し、届かなければInteractivity設定とSlack側の配信状況を調べる。実通知が届いたら検索候補、画面遷移、Options Load URLを確認する。
2. 専用の試験レコードを用意してから書き込みを本人限定で有効化し、案件・アクションの保存、DM、Notionと各同期先を読戻す。
3. Gmailは必要な認可・秘密値・定期トリガーを揃えた時点から14日観測を始め、その後3日並走を満たして切替判断する。Notionは項目別同期の実Webhookを限定確認する。

見送った案: 操作通知が届かない状態で書き込みを有効化しない。実顧客レコードを試験に使わない。旧476件の修復と3施設の統合は行わない。
