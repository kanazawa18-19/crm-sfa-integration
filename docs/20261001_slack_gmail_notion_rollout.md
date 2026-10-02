# Slack操作・Gmail GAS移管・Notion項目別同期（2026-10-01）

## 状態

| 対象 | 実装 | 実物確認 | 本番反映 |
|---|---|---|---|
| Slack | 本人DMの取引先検索を用意。登録・更新は業務範囲未決定 | Slack実アプリでは未検証 | 未実施 |
| Gmail | GAS取得・CRM取込み・再送台帳・比較スクリプトを用意 | 実Gmail/GAS/DBでは未検証。並走未開始 | 未実施 |
| Notion | 項目別時刻と既存時刻の移行基準を用意 | 実DB・実Notion通知では未検証 | 未実施 |

## 2026-10-01 本番反映後の確認

本人の「いいよ、進めて」を受け、`40fb9f2` を main に push。API と管理画面は両方 READY、GitHub CI `36845533043` は成功。DB の新規2移行を先に適用し、Prisma の40移行すべて適用済みを再確認した。API の `/healthz` は 200/ok。以下の表は冒頭の配備前状態を更新する。

| 対象 | 確認した実物 | 残る条件 |
|---|---|---|
| Slack | API側に本人ユーザー・所属ワークスペースの許可値と画面URLを本番設定。既存Slackアプリの `/crm` 登録画面に送信先と説明を入力 | 保存は自動審査が拒否。恒久的なコマンド追加と場合によるBot User追加の具体承認が必要。本人DMでの実操作は未検証 |
| Gmail | 本番APIに専用受信秘密値と担当メールを設定。実APIへ架空IDを `dry_run:true` で送り 200/unmatched、DBの保存0件を確認。GASの非共有プロジェクトを本人アカウントで作り、`ingest.js` と同一のコードを保存。10/2に `appsscript.json` へGmail高度なサービスと必要な3権限を保存し、画面を再読込して内容を確認 | Script Properties・本人の実行認可・5分トリガーは未完了。Gmailサービスの実呼出しは未検証。14日観測は未開始。秘密値はmacOSキーチェーン `crm-sfa-gmail-ingest-secret` に保存し、Git/Vaultには置かない |
| Notion | 本番DBの `RecordSyncFieldWatermark` と `GmailIngestEffect` を実読確認。前者に合成キーを使い、同時刻未完了の再試行・完了後の拒否・別項目の古い更新受理をトランザクション内で確認し、rollback後の残存0件を確認。10/2の読取では直近24時間のNotion受信記録68件、項目別台帳は取引先マスター2レコード・計8行（完了5、未完了3）。ログでは未完了3項目がZohoへの新規レコード作成不可によるスキップ、Notionとシートへの反映は記録済みと確認 | 自然通知から項目別台帳に記録された可能性は高いが、個々のWebhook内容と同期先を照合した限定試験は未実施。未完了の実顧客レコードを自動作成・再配送しない |

Slackのコマンド保存は自動審査の拒否を迂回しない。GASの秘密値をブラウザへ入力する操作は Chrome 操作手順で本人への引き渡しが必要。Gmail読取権限の認可は本人アカウントの画面で行う。14日の開始は、dry-runトリガーの自然実行を実測してから記録する。

## Slackの最小操作案

**2026-10-02 追記：** 本人の意図はSalesforce連携に近い、携帯のSlack内で案件・アクションを閲覧・更新する体験。以下の `/crm` 検索は旧最小案で、完成形として登録しない。新しい画面・連携・導入順は [モバイル中心の設計案](slack_mobile_crm_design.md) を正とする。

`/crm ホテル名` を本人DMで実行すると、取引先名・識別用ID・CRM画面へのリンクを最大10件返す。署名検証に加え、`SLACK_CRM_ALLOWED_USER_IDS`（SlackユーザーIDのカンマ区切り）と `SLACK_CRM_ALLOWED_TEAM_ID` で利用者とワークスペースを絞る。結果は本人にだけ見える一時表示。CRM画面のURLは `DASHBOARD_BASE_URL` を使う。未設定時はID付きの名前だけ返す。Slack側のスラッシュコマンドのRequest URLは `/api/webhooks/slack-crm-command`。

更新機能の案：最初は「取引先の検索 → 1件を選択 → 変更項目・変更前後をプレビュー → 確定」で始める。対象DBと項目の許可リスト、利用者、登録可否を本人が決めてから書込経路へ接続する。再送時の二重更新を避けるため、操作IDと対象の版を保存する。全DB自由更新や施設統合はこの案に含めない。Slackアプリへのコマンド追加と本人DMでの試験は、本番URLを公開する前の設定・指示が必要。

## Gmailの設定と観測

GASソースは `gas/gmail-ingest/`。担当者本人の非共有プロジェクトに置き、高度なGmailサービスを有効化する。Script Propertiesは `CRM_INGEST_URL`、`CRM_INGEST_SECRET`、`REP_EMAIL`、`DRY_RUN=true`。API側に `GMAIL_INGEST_WEBHOOK_SECRET` と `GMAIL_INGEST_REP_EMAIL` を同じ担当者専用に設定する。秘密値はコード・Vault・ログへ書かない。`installGmailIngestTrigger` で5分トリガーを1本登録する。既存Push・日次安全網は切替判断まで維持する。

GASは10通ずつ送る。全ページが成功したときだけ成功時刻を進め、途中失敗・4分超過ではページ位置から再開する。dry-runと本取込みの成功時刻は別。実取込みを始める直前に `prepareGmailIngestLive` を一度実行し、Push稼働中の現在時刻を基準にする。未実行ならGASは実取込みを拒否する。重複IDはCRMの一意制約で抑える。1通の後処理失敗は `effect_failed` として分離し、`FAILED_MESSAGE_IDS` から毎回10件ずつ再試行する。未解決IDは切替不可。GASログの `results` はID・判定・Gmail内部時刻だけで、件名・本文・宛先を含めない。

並走を始めた時刻を記録し、**14日を経過するまで合格としない**。毎日のGASログをJSONLへ取り出し、`scripts/compare_gmail_gas_ingest.py` に同じ担当者・期間を指定して実行する。`gas_missing_in_crm` と `crm_not_seen_by_gas` の両方向、`unmatched_gas_ids`、`effect_failed_ids`、走査範囲の不足を調べ、件数だけでなく各IDの理由を照合する。Pushに未取込のメールがあれば安全な補完手順を作り、対象期間を再比較する。到着からGAS受付までの遅延・トリガー失敗・GAS実行時間も記録する。旧案の「would_insert=0なら合格」は採用しない。

切替条件は①14日間の対象期間を観測、②同じ範囲のGAS/CRM ID集合の差分を理由付きで解消、③遅延と実行失敗を実測し営業上許容できること、④本人の遅延許容と本番切替指示。条件が揃った後、GASを実取込みへ変え、少なくとも3日間は既存Pushと併用して、保存IDと通知の二重化がないか確認する。3日も未経過なら完了扱いにしない。その後にPush購読停止を別途指示で実施する。戻す際はGASトリガーを停止してPushを維持する。

`GmailIngestEffect` はGASが保存したメール履歴のNotion更新を再送で再開する。Notionの最終メール日時は連絡先単位で処理を直列化し、EmailLogの最大日時を使う。既存Pushが先に保存してNotion更新だけ失敗した場合はGASの台帳が無く、自動回復しないため、比較と個別調査が必要。通知は送信前に一回だけ試行印を付け、HTTP応答不明時に自動二重送信しない。送信に失敗した場合は自動再送をせず、ログと送達先を人が照合してから判断する。これは「送達保証」ではない。実サービスで送達と印の整合を確かめるまで本番切替しない。

## Notion項目別同期の移行

`RecordSyncFieldWatermark` を追加する。新コードが各レコードで最初にイベントを処理する直前に、旧 `RecordSyncWatermark` と `IdMapping` の時刻を `__baseline__` 行へ一度だけ固定し、切替前の古い通知の再適用を防ぐ。切替後の別項目は各項目の受理・完了時刻で比較する。同じ項目の古い通知は落とし、同時刻でも未完了なら再試行する。既存のレコード単位のロックは保持する。先にDB移行、その後アプリ反映が必要。移行前に落ちた通知は項目情報を復元できないため、別途差分照合で補う。

隔離DBで、A→Bの順序逆転・同時刻・重複・部分失敗後の再送・他ツールの新しい同項目を確認する。Notionの生Webhook `updated_properties` と、取得したページ値を使う通常入口も限定実物で確認する。試験で作ったデータは片付ける。旧476件の修復は再実行しない。斉木別館・千年亭・松涛園は別管理を維持する。

## 参考にした現行Google仕様

- [Gmail APIの検索](https://developers.google.com/workspace/gmail/api/guides/filtering)：`after:` / `before:` に秒単位のUnix時刻を使える。
- [Apps Scriptの高度なGmailサービス](https://developers.google.com/apps-script/advanced/gmail)：`messages.list` 後に `messages.get` で詳細を読む。
- [Apps Scriptの上限](https://developers.google.com/apps-script/guides/services/quotas)：トリガーの日次実行時間などにはアカウント種別による差がある。実行量は未計測。
