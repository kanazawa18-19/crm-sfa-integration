# web-engagement-tool 連携まとめ（実装ノート）

別リポジトリ `web-engagement-tool`（CNCTOR JAPANのホテル/旅館向けオンサイトエンゲージメント
ツール、Next.js）との連携は、2026-08-13時点で以下の3系統が存在する。今後この連携に
手を入れる・拡張する前に、既存実装の有無を確認する目的でこのノートを作る（過去に
outbound方向の重複実装が22コミット分気づかれずに作られていたことがあるため）。

## 系統一覧

| # | 方向 | 内容 | 実装 | 配線 |
|---|------|------|------|------|
| 1 | outbound | 案件管理DBの「次回アクション日」変更 → Google Calendar連携 (`POST /api/calendar/events`) | `src/calendar_sync/` | `notion_webhook.handler_with_proxy`の`calendar_sync`引数（`production_wiring.build_calendar_sync_callable`） |
| 2 | outbound | 連絡先DB（Contact）の作成・更新 → リード管理 (`POST /api/leads/sync`) | `src/lead_sync/`（`WebEngagementToolLeadSyncClient`） | `notion_webhook.handler_with_proxy`の`lead_sync`引数（`production_wiring.build_lead_sync_callable`） |
| 3 | inbound | web-engagement-tool側のリードのホットリード化・新規識別通知 (`POST /api/webhooks/web-engagement`) | `src/sync_engine/webhook_handlers/web_engagement_webhook.py` | `src/api/app.py`の専用エンドポイント（`Dispatcher`は経由しない） |
| 4 | inbound | Googleカレンダー予定 → 商談アイテム(アクション履歴)自動作成、Slack承認フロー付き (`POST /api/webhooks/web-engagement-meeting` → Slack DM承認 → `POST /api/webhooks/slack-interactions`) | `src/meeting_sync/`（`matcher.py`/`action_type.py`/`slack_approval.py`）、`src/sync_engine/webhook_handlers/web_engagement_meeting_webhook.py`・`slack_interaction_webhook.py` | `src/api/app.py`の2エンドポイント（`Dispatcher`は経由しない） |

系統4はweb-engagement-tool仕様書T-20「カレンダー連携×商談ログ自動化」の実装（2026-08-13）。
web-engagement-tool側のcron（`calendarMeetingSync.ts`、AppSettings.calendarMeetingSyncEnabled
がONの場合のみ実行）が各営業担当のGoogleカレンダーを走査し、参加者に社外メールを含む
直近イベントの生データをそのままPOSTする。crm-sfa-integration側で連絡先DB→取引先→
「進行中」案件へのマッチングを行い、1件に絞れた場合のみ担当営業（`rep_email`）へSlack DM
で承認依頼を送る。DMの承認ボタンが押されたときのみ、実際にNotionアクション履歴DBへ
新規ページを作成する（即時登録はしない）。

**Phase 2（Gemini議事録取込み、2026-08-14実装完了）**: Googleカレンダーイベントの
`attachments`フィールド（Geminiが会議後に議事録・録画を添付する）から`document_url`を
拾い、承認時にNotionの「議事録・録画リンク」プロパティへ書き込む。追加のOAuthスコープ・
再連携は不要（Calendar APIに添付ファイル専用のスコープは存在しない、2026-08-14に
Googleの公式スコープ一覧で確認済み）。web-engagement-tool側`extractDocumentUrl()`が
http(s)形式の検証と複数attachment時の警告ログを行い、crm-sfa-integration側
`slack_approval.py`も長さ上限・URL形式の二重チェックを行う（Notion書き込み失敗で
案件登録全体が失敗し続ける事故を防ぐため）。**Notta連携（Phase 3）は引き続き未実装**
（APIキー未取得のため保留）。

系統1・2は同じ設計パターン（クライアントクラス + `notion_webhook.handler_with_proxy`への
callable注入）、系統3は`kintone_webhook.py`/`zoho_webhook.py`と同じraw Lambda風ハンドラの
パターンを踏襲している。系統1・2は**実際のNotion API Webhookが発火した時点**（人間による
手動編集・Dispatcher自身によるAPI経由の書き込みの両方を含む）で呼ばれるため、
「Notion連絡先DBへの変更なら発生源を問わずweb-engagement-tool側へ伝わる」設計になっている。

## 環境変数の対応関係

`config/.env.example`参照。web-engagement-tool側は`.env.example`に対になる変数がある。

| crm-sfa-integration | web-engagement-tool | 用途 |
|---|---|---|
| `WEB_ENGAGEMENT_TOOL_URL` | (自分自身のURL) | 系統1・2の送信先ベースURL（共用） |
| `CALENDAR_SYNC_API_TOKEN` | `CALENDAR_SYNC_API_TOKEN` | 系統1の認証 |
| `CRM_SFA_SYNC_API_TOKEN` | `CRM_SFA_SYNC_API_TOKEN` | 系統2の認証 |
| `WEB_ENGAGEMENT_WEBHOOK_SECRET` | `CRM_SFA_WEBHOOK_URL` / `CRM_SFA_WEBHOOK_SECRET` | 系統3の認証（web-engagement-tool側が送信先URL・シークレットを持つ） |
| `WEB_ENGAGEMENT_MEETING_WEBHOOK_SECRET` | `CRM_SFA_MEETING_WEBHOOK_URL` / `CRM_SFA_MEETING_WEBHOOK_SECRET` | 系統4前段（カレンダーイベント通知）の認証 |
| `SLACK_BOT_TOKEN` / `SLACK_SIGNING_SECRET`（crm-sfa-integration専用、web-engagement-tool側の同名変数とは別） | — | 系統4後段（Slack DM承認・interactivityコールバック）の認証。Slack App側で`chat:write`/`users:read.email`/`im:write`スコープとInteractivity設定（Request URL）が別途必要（手動設定） |

## 既知の制約・今後の検討事項

- **系統3の書き込みは、既存プロパティ（sync_scope=ALL_TOOLS）を直接書いてはいけない**。
  `web_engagement_webhook.py`は`携帯番号`のような既存の同期対象プロパティを直接
  Notionへ書かない（2026-08-13、shirokuma-secレビューBLOCKER対応で削除）。理由: 系統3の
  書き込みも実際のNotion API Webhookを発火させ、`dispatcher.dispatch()`に「Notion発の変更」
  として届く。Notion発の変更は常にマスターとして無条件伝播される設計（コンフリクト判定
  なし）のため、web-engagement-tool側の未検証な入力（フォーム等）でZoho/kintone側の
  正しいデータを上書きしてしまう。系統3が書き込んでよいのは`リードスコア`/
  `ホットリード化日時`/`Web接客ツールURL`のような、この連携専用のNOTION_ONLYプロパティに限る。
- **系統3の書き込みは系統2のecho-backを誘発する**。系統3が連絡先DBのNotionページを
  更新すると、実際のNotion API Webhookが発火し、`db_key=="contact"`である以上
  `handler_with_proxy()`は無条件で系統2（`lead_sync`）も呼ぶ。つまりweb-engagement-tool側から
  届いたホットリード通知が、Notion経由で一往復してweb-engagement-tool側の
  `POST /api/leads/sync`へまた戻っていく。web-engagement-tool側の`/api/leads/sync`は
  `skipCrmSfaNotify: true`で受けるため無限ループにはならないが（2026-08-13、
  web-engagement-tool側shirokuma-sec/obasan-qualityレビューで確認済み）、無駄な往復
  呼び出しが発生する。実害が顕在化するようであれば、`notion_webhook.py`側で
  「変更されたプロパティが系統3専用のNOTION_ONLYプロパティのみの場合は`lead_sync`呼び出しを
  スキップする」といったガードの追加を検討すること。
- **系統3（inbound webhook）には再送・自己修復の仕組みがない**。web-engagement-tool側からの
  通知はfire-and-forget前提で、Notion API側の一時的な障害・レート制限等でこのWebhookが
  500を返した場合、web-engagement-tool側がリトライしなければホットリード化の通知は失われる。
  営業機会の見逃しに直結しうるため、web-engagement-tool側でリトライが保証されているか確認する
  か、保証がなければ定期的な補完バッチ（直近のホットリードを定期取得してupsertし直す等）の
  追加を検討する価値がある。
- **担当営業の受け皿が無い**。連絡先DBには担当営業に相当するプロパティが現状存在しないため、
  系統2・3どちらも`assigned_rep_email`相当のデータは書き込まれない（受け取っても捨てる）。
- **系統3経由の携帯番号はNotionへ恒久的に反映されない**（2026-08-13、Gemini他モデルレビュー
  で指摘）。上記BLOCKER対応の裏返しとして、web-engagement-tool側で取得した携帯番号情報は
  この連携経由では一切Notion連絡先DBに載らない。もし今後この情報を活かしたくなった場合は、
  `携帯番号`とは別のNOTION_ONLYな受け皿プロパティ（例:「Web接客ツール携帯番号」）を新設し、
  そちらへ書く形にすること（既存の`携帯番号`プロパティへは書かない）。
- **Webhook認証は共有シークレット方式のまま**（2026-08-13、Gemini他モデルレビューで
  HMAC署名+タイムスタンプ窓によるリプレイ対策を提案された）。既存のkintone/zoho/
  spreadsheet Webhookも含めてプロジェクト全体が`X-Webhook-Secret`共有トークン方式
  （`_common.py`のBLOCKER7暫定実装）で統一されており、この連携だけHMACに変えると
  かえって一貫性が崩れるため今回は見送った。プロジェクト全体でWebhook認証方式を
  見直す機会があれば、この連携も合わせて更新すること。
- **系統4で自動作成されたアクション履歴もZoho/kintoneへ伝播しない**（系統3と同じ理由。
  `Dispatcher.dispatch()`が新規レコード作成に未対応、`dispatcher.py`のコメント参照）。
  Slack承認を経てもNotionにのみ存在する状態が続く。
- **系統4のマッチング精度はNotion連絡先DBのメールアドレス登録網羅性に依存する**。参加者の
  メールアドレスが連絡先DBに未登録（名刺交換未登録等）の場合は0件スキップとなり、実際には
  商談であっても自動検知されない（意図した仕様。誤登録より見逃しを許容する方針）。
- **系統4のアクション種別判定はカレンダータイトルの表記ゆれに弱い**。実運用で「【商談
  （訪問）】」以外の書式が使われていた場合は判定できず、Meetリンクの有無でフォールバックする
  （`src/meeting_sync/action_type.py`）。
- **系統4のcronはデフォルトで無効**（`AppSettings.calendarMeetingSyncEnabled`、既定False）。
  web-engagement-tool側の`/admin/calendar`から管理者（master権限）がON/OFFできる
  （2026-08-13、obasan-qualityレビューBLOCKER対応でトグルUIを追加済み）。
- **Slack App側の手動設定が前提**。Interactivity & Shortcuts の Request URL 設定、
  Signing Secret の取得、Bot Token への`chat:write`/`users:read.email`/`im:write`
  スコープ付与は Slack管理画面（api.slack.com/apps）でユーザー自身が行う必要がある
  （コードでは自動化不可）。**2026-08-14、既存アプリ「sales-crm-sfa」で全項目設定完了**
  （Socket Modeが有効だとRequest URL欄が出ないため、先にSocket Modeを無効化する必要が
  あった。既存Incoming Webhookのチャンネル`#sales-log`を維持したまま再インストール済み）。
- **web-engagement-tool側の手動セットアップ（Notion プロパティ追加・両リポジトリの
  Vercel環境変数・管理画面トグル）も2026-08-14に完了**。系統4は本番で稼働中。
- **手書きPrismaマイグレーションはbuildスクリプトに`prisma migrate deploy`を組み込む
  までは本番DBに反映されない**（2026-08-14、実際にこれが原因でweb-engagement-tool側が
  全ルート500エラーになる障害を起こした。`calendarMeetingSyncEnabled`カラム追加の
  マイグレーションファイルをリポジトリに置いただけでデプロイし、middlewareの
  AppSettings参照が本番DBに存在しないカラムをSELECTして失敗した）。
  `web-engagement-tool/package.json`の`build`スクリプトに`prisma migrate deploy && next build`
  を追加して修正済み。今後この構成（DB直接アクセス不可のため手書きマイグレーション）の
  プロジェクトでスキーマ変更を行う際は、このbuildスクリプトが継続してマイグレーションを
  適用する前提を崩さないこと。
- **Phase 2で議事録リンクが早期承認だと反映されないケースがある**（2026-08-14、
  obasan-qualityレビューWARN指摘）。会議終了直後にDMが届き、Geminiが議事録を
  attachmentsへ反映する前に営業担当が承認してしまうと、Notionページは
  `議事録・録画リンク`無しで作成される。`handle_interaction()`は既存ページが
  見つかった場合は「登録済み」として何もしない設計（重複作成防止）のため、後日の
  cronで同じイベントが`document_url`付きで再送されても、既存ページへの反映は行われない
  （手動でNotion側に追記する必要がある）。低頻度（cronは日次1回）だが、既知の制約として
  記録しておく。
- **承認は担当営業本人へのSlack DMで行い、Notionの確認フローは無い**（2026-08-13、
  金沢さん要望でチェックボックス方式からDM承認方式へ変更）。DM送信の成否は
  `post_approval_request()`が戻り値で返し、失敗時は`SLACK_WEBHOOK_URL_ALERT`
  （運用アラート用Incoming Webhook）へフォールバック通知する
  （obasan-qualityレビューBLOCKER対応: 以前は失敗がログにしか残らず、案件が
  誰にも気づかれずNotion未登録のまま失われるリスクがあった）。承認ボタン押下時は
  `payload["user"]["id"]`とDM解決時のSlackユーザーIDを突合し、担当営業本人以外の
  操作を拒否する（shirokuma-secレビューWARN対応、多層防御）。
- **却下は永続化されない**。「対象外」にしても記録が残らないため、同じ予定が次回の
  カレンダー同期で再度検知されると改めてDM承認依頼が届くことがある
  （obasan-qualityレビューWARN指摘、Slackメッセージ本文にもその旨を明記）。
- **承認ボタンの同時押下（TOCTOU）で二重登録される可能性がある**（shirokuma-secレビュー
  WARN指摘）。低確率のエッジケースとして許容し、専用のロック機構等は導入していない。
