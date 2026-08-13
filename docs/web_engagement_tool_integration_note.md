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
