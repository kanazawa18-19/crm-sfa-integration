# Slack携帯CRMの実装と導入手順（2026-10-02）

## 現在地

Slackの本人用App Homeから案件を探し、案件の営業ステータス・次回アクション日・確度、アクションの登録・種別・日付・短いメモを入力する実装を用意した。既存アクションの編集もできる。保存前に変更前後を表示し、受付後の結果は本人DMへ送る。Notionを正本として更新し、既存の同期経路へ渡す。

コード、ローカルの単体試験、使い捨てPostgreSQLによる台帳試験は済み。2026-10-02に本番DB移行とAPI配備、SlackアプリのHome/Event/Options設定を反映した。本人のSlack画面でApp Homeと2つのボタンを確認した。10/3までに本人が携帯で案件選択画面を確認し、操作WebhookのHTTP 200も確認した。こちらのブラウザでも案件選択→検索候補10件→案件操作メニューまで表示を確認した。**実Notionへの変更、各CRMへの到達は未検証**。`SLACK_CRM_WRITE_ENABLED` は未設定で書き込み無効。Gmailの14日観測や3日並走が経過したという意味にはならない。

## 携帯での流れ

```
本人のSlack「sales-crm-sfa」ホーム
  ├─ 案件を探す ─▶ 案件名・取引先名で選ぶ ─▶ 案件を更新
  │                                     ├─ アクションを記録
  │                                     └─ アクションを直す
  └─ アクションを記録 ─▶ 案件を選ぶ ─▶ 種別・日付・メモ
       ↓
    変更前後の確認 ─▶ 保存 ─▶ 受付番号 ─▶ 本人DMで結果
```

ホームの「最近更新した案件」は本人がこの画面から更新した案件を最大3件表示する。担当者と案件の対応が未確定のため「今日の予定案件」はまだ表示しない。共有チャンネルには投稿しない。長いメモや契約・失注などの終端状態はこの画面で編集させない。

## 安全策と失敗時の扱い

- Slack署名、チームID、許可した利用者IDをサーバで検証する。入力画面の値もサーバ側の許可項目と候補で再検証する。
- 操作IDを保存し、同じ保存操作が再送されても1回だけ受け付ける。アクション作成の応答不明は自動再作成せず、台帳とNotionの照合が必要な状態にする。
- 保存前の値とNotionの現在値が違う場合は上書きせず競合として止める。同一レコードへの既存同期とは同じPostgreSQLの鍵を使う。
- Notion更新後の同期先で版の競合や一部未反映があれば `partial` として本人に知らせ、古い値で自動再配送しない。一時的な例外だけ最大3回再試行する。
- 保存処理はDBに残した操作を処理し、5分ごとの回収口も用意した。Slackイベントの受領後にホーム描画を動かす方式はVercel実環境で未検証。DM通知が12回失敗した場合は台帳に未通知として残る。

## 反映時の順序

1. `dashboard/prisma/migrations/20261002000000_slack_crm_operation/migration.sql` を本番DBへ適用し、`SlackCrmOperation` が存在することを読み取りで確認する。
2. APIを反映し、既存の `SLACK_SIGNING_SECRET` と `SLACK_BOT_TOKEN` を再利用する。本人のSlack IDを `SLACK_CRM_ALLOWED_USER_IDS`、チームIDを `SLACK_CRM_ALLOWED_TEAM_ID` に設定する。最初は `SLACK_CRM_WRITE_ENABLED` を未設定のままにする。秘密値をGitやVaultへ書かない。
3. Slackアプリ `sales-crm-sfa` のApp Homeと本人DMを閲覧できるMessagesタブをON、Event SubscriptionsをONにし、Request URLを `https://crm-sfa-integration.vercel.app/api/webhooks/slack-crm-events`、Bot Eventsに `app_home_opened` を設定する。Messagesタブは閲覧専用にする。Interactivityの既存Request URLは維持し、Options Load URLを `https://crm-sfa-integration.vercel.app/api/webhooks/slack-interactions` に設定する。現時点で追加のOAuthスコープや認証交換は必要ない。
4. 本人のホーム表示、検索、各モーダルと選択肢を携帯のSlackで確認する。書き込みが無効な間の保存は「準備中」を返す。
5. 専用の試験レコードだけで書き込みを有効にし、案件1件の更新、アクション1件の作成と編集、二重送信・競合・本人DM、Notionと同期先の読戻しを行う。試験の作成物は片付ける。一般の本番レコードへ広げるのはその後。

既存の `/crm` Slash Commandは未登録のまま。このモバイル画面には不要。旧476件の修復は再実行せず、斉木別館・千年亭・松涛園は引き続き別施設として扱う。

## 確認した場所

- 操作と判断: `src/slack_crm/`
- 受信口と定期回収: `src/api/routes/webhooks.py`、`src/api/routes/cron.py`、`vercel.json`
- 台帳: `dashboard/prisma/schema.prisma` と上記migration
- 試験: `tests/slack_crm/test_mobile_flow.py`、`tests/api/test_route_registry.py`
- 独立レビュー・他モデルレビュー: `docs/evidence/slack-mobile-20261002/`
- 本番反映と実画面の確認結果: `docs/evidence/slack-mobile-20261002/live-validation.md`
