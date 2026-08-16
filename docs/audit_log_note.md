# データ監査ログ（AuditLog）

2026-08-17、金沢さんからの「いずれのデータも最終編集者・作成者・最終編集日時・作成日時、
いつどんな編集が行われたかを記録したい」という要望に対応して追加した。ガバナンス・
説明責任目的で、対象は本プロジェクトが管理する業務データ全般（取引先マスター/連絡先/
案件管理/アクション履歴/サービス商品/チェーンの6DB、いずれもNotion）。

## 対象範囲の制約（重要）

以下の2種類の変更は、いずれも「このバックエンドを経由しない」という共通の理由により**原理的に
監査ログの対象外**（実装の不備ではなく、設計上どうやっても捕捉できない）:

1. **Notion管理画面から人間が直接編集した変更。** Notion自体にフィールド単位の変更履歴を返す
   APIが無く、このコードを経由しない変更のため技術的に捕捉不可能。
2. **`SyncScope.NOTION_ONLY`として登録されているプロパティへの変更。** 例えば案件管理DBの
   「申込書・契約書」「見積書」「提案資料」（いずれも`src/db_schema/project.py`、ファイル型・
   Any-to-Any同期の対象外）、連絡先DBの「名刺交換日」「Eight連携ID」（`src/db_schema/contact.py`、
   Any-to-Any同期の対象外の社内運用項目）等。これらはAny-to-Any同期の対象外という設計上、
   このバックエンドが書き込む経路自体が存在しない（Notion管理画面から人間が直接入力する
   運用が前提のプロパティ）ため、(1)と同じ理由で対象外になる。

dashboard `/audit-log`画面にも同旨の注記を表示している（`dashboard/app/(dashboard)/audit-log/page.tsx`）。

## フック箇所

Notionへの全書き込み（ページ作成・プロパティ更新）は、最終的に`HttpNotionClient`
（`src/sync_engine/clients/notion_client.py`）の`create_page()`/`update_page()`の2メソッドへ
集約される（kintone/Zoho/Web接客ツール/lead-researcher/Slack承認/Gmail同期/一括移行の各
呼び出し元は、いずれも直接または`NotionSyncTarget`/`_MultiDbNotionSyncTarget`
（`production_wiring.py`）経由でこの2メソッドを呼ぶ）。このため監査ログの記録ロジックは
この2メソッドのみに実装し（`src/audit_log/recorder.py`の`record_notion_write()`を呼ぶ）、
個々の呼び出し元へ手動でログ呼び出しを仕込む方式は採らなかった（書き漏れリスクを避けるため）。

`archive_page()`（Notionの論理削除相当）は今回のスコープ外（要件が`action: "create"|"update"`
のみを対象としているため）。

## actorSourceの伝播方式（contextvars）

`HttpNotionClient`は1インスタンスが複数の呼び出し経路から共有されうる
（例: `production_wiring.py`が構築するNotionクライアントは、kintone Webhook経由・Zoho
Webhook経由のどちらの書き込みでも同じインスタンスが使われる）。そのため「どの経路からの
書き込みか」をクライアントのコンストラクタ引数として固定することができず、
`src/audit_log/actor_context.py`の`set_actor()`（`contextvars`ベース）を各エントリポイントで
withブロックとして被せ、実際にNotionへ書き込む瞬間まで暗黙に伝播させる設計にした。

`concurrent.futures.ThreadPoolExecutor`で並列実行される経路（`src/migration/migration_pipeline.py`
の一括移行）は、contextvarsが呼び出し元スレッドからワーカースレッドへ自動伝播しないため、
実際に`create_page()`を呼ぶ関数自身の中で`set_actor()`する（詳細は同ファイルのコメント参照）。

現在`set_actor()`を仕込んでいるエントリポイントとactorSourceの対応:

| actorSource | エントリポイント |
|---|---|
| `kintone_webhook` | `src/sync_engine/webhook_handlers/kintone_webhook.py` `handler()` |
| `zoho_webhook` | `src/sync_engine/webhook_handlers/zoho_webhook.py` `handler()` |
| `spreadsheet_webhook` | `src/sync_engine/webhook_handlers/spreadsheet_webhook.py` `handler()` |
| `web_engagement_webhook` | `src/sync_engine/webhook_handlers/web_engagement_webhook.py` `handler()` |
| `lead_inquiry_webhook` | `src/sync_engine/webhook_handlers/lead_inquiry_webhook.py` `handler()` |
| `slack_interaction_webhook` | `src/sync_engine/webhook_handlers/slack_interaction_webhook.py` `handler()`（`meeting_sync/slack_approval.py`の`handle_interaction()`経由のcreate_pageも含む） |
| `gmail_sync` | `src/gmail_sync/sync.py` `_process_message_ref()`（`sync_rep()`/`sync_rep_incremental()`/`sync_all()`いずれの経路でも共通） |
| `migration` | `src/migration/migration_pipeline.py` `materialize()`内`_check_create_and_register()` |

調査の結果、以下は現時点でNotionへの書き込みを行わないため`set_actor()`は不要と判断した:
- `notion_webhook.py`（Notion発イベントはNotion自身への書き込み対象から自己除外されるため）
- `web_engagement_meeting_webhook.py`（Slack承認依頼を投稿するのみで、この時点ではNotionへ書き込まない）
- `gmail_push_webhook.py`（実体の書き込みは`gmail_sync/sync.py`側で発生し、上表で既にカバー済み）
- dashboard（Next.js）側: `grep -rn "notion" dashboard/lib dashboard/app`で確認した限り、
  dashboard側からNotion APIへ直接書き込むコードは無い（`notion_page_id`を識別子として
  参照するのみ）。dashboard向けのバックエンドAPI（`src/api/app.py`）も現状GETエンドポイント
  のみで、Notionへの書き込みを伴うPOST/PATCHエンドポイントは無い。将来dashboard発の書き込み
  経路が追加された場合は、そのエントリポイントで`set_actor("dashboard", label=<ログインユーザー
  のメール>)`を仕込むこと。

`set_actor()`で囲まれていない状態でNotion書き込みが発生した場合（新しい書き込み経路の
追加漏れ等）は、`actorSource="unknown"`のままwarningログを残して記録する（サイレントに
経路不明のまま記録され続けることへの気づきを残すため。`recorder.py`の`get_actor()`使用箇所
参照）。

### actorLabel（2026-08-17、obasan-qualityレビューWARN対応）

既に手元にある情報から追加コスト無しで解決できる範囲で、`actorLabel`に人間が分かる識別子を
渡すようにしている:

- `kintone_webhook`: kintoneレコードの「更新者」（無ければ「作成者」）フィールドの表示名
  （`_kintone_actor_label()`）。
- `slack_interaction_webhook`: 承認ボタンを押したSlackユーザーID（`payload["user"]["id"]`。
  `handle_interaction()`自身が本人確認のため既に参照している値と同じ）。
- `gmail_sync`: 同期対象の営業担当のメールアドレス（`rep_email`。`EmailLog.repEmail`に
  記録している値と同じ）。

`zoho_webhook`/`spreadsheet_webhook`/`web_engagement_webhook`/`lead_inquiry_webhook`/
`migration`は、ペイロード側に相当する「誰が」の情報が無いため`actorLabel`は未設定（`None`）
のまま。

## update時の差分抽出

`update_page()`は、実際のPATCH送信前に対象ページの現在値を`get_page()`で1回読み直し
（`_fetch_current_values_for_audit()`）、書き込もうとしているプロパティのうち実際に値が
変わったものだけを`changedFields`へ記録する。読み直しに失敗した場合（ページ削除・権限
エラー・API障害等）は、誤った内容を記録するより安全側に倒し、その回のupdateについては
監査ログの記録自体をスキップする（本来のPATCH処理には影響させない）。

この読み直しにより、`update_page()`呼び出しごとにNotion APIへのリクエスト数が実質倍になる
（PATCH 1回につきGET 1回追加）。Notion APIのレート制限は実測でおおむね平均3req/秒程度であり、
本プロジェクトの書き込み頻度（Webhook契機の逐次処理が中心）に対しては許容範囲と判断した。
大量バルク処理（一括移行）は`create_page`のみを使いこの読み直しの対象外のため影響しない。

## RELATION/USER型プロパティの表示名解決（2026-08-17、obasan-qualityレビューWARN対応）

案件の「取引先」relationや「担当メンバー」（USER）等が変わった場合、`changedFields`に
Notionの生ページID/ユーザーIDだけを記録すると、金沢さんが見ても何が変わったか分からない
（`["a1b2c3d4-..."] → ["e5f6..."]`）。そのため`src/sync_engine/clients/notion_display_resolver.py`
の`resolve_display_values()`が、記録直前にRELATION型を参照先ページのタイトルへ、USER型を
`NotionUserDirectory`（`src/api/user_directory.py`、既存）経由でユーザー表示名へ解決する。
解決した値はあくまで`AuditLog.changedFields`用の表示値であり、Notion API本体への実際の
書き込みには一切使わない（生のIDのまま送信する）。

コスト面の判断: RELATION解決は参照先ページ1件につき`GET /v1/pages/{id}`を1回追加で呼ぶ
（`update_page`の「変更前値取得のための追加GET」と同種のコスト）。通常1回の書き込みで
変更されるRELATIONプロパティの値は数件程度に留まるため許容範囲と判断したが、
`src/migration/migration_pipeline.py`の一括移行（最大148,000件規模）でこれを行うと
Notion APIリクエスト数が大きく膨らむため、`actorSource="migration"`の場合は解決自体を
スキップし、生のページIDのまま記録する。また解決に使うNotionクライアントのタイムアウト・
リトライ予算は、calendar_sync/lead_syncの副次連携フックと同じ`HOOK_TIMEOUT_SECONDS`/
`HOOK_MAX_RETRIES`（短め）を使い、監査ログの表示名解決のために本来の書き込みレスポンスを
数秒〜数十秒規模で遅延させないようにしている。

解決に失敗した場合（対象ページ削除済み、`NOTION_API_KEY`のIntegrationに「ユーザー情報の
読み取り」権限が付与されていない等）は、生のIDのままフォールバックする（監査ログの記録
自体は諦めない）。

## 保存先・スキーマ

`AuditLog`テーブル（Neon Postgres、`dashboard/prisma/schema.prisma`でスキーマ管理）。
既存の`RepGmailConnection`/`EmailLog`（`src/gmail_sync/db.py`）と同じパターンで、
Pythonバックエンド側は`psycopg`で直接INSERTする（`src/audit_log/db.py`）。

カラム: `id`, `dbKey`（"client_master"|"contact"|"project"|"action"|"product"|"chain"）,
`notionPageId`, `action`（"create"|"update"）, `changedFields`（JSON、フィールド名→
`{"before": ..., "after": ...}`）, `actorSource`, `actorLabel`（省略可）, `createdAt`。

## 閲覧UI

dashboard（Next.js）側に`/audit-log`を新設し、`dbKey`/`actorSource`/日付範囲でフィルタできる
一覧画面を用意した（`dashboard/app/(dashboard)/audit-log/page.tsx`）。Prisma経由で直接
`AuditLog`を参照する（既存の`users`/`settings/email-reminders`ページと同じ、Server Component
から`prisma`クライアントを直接使うパターン）。「対象ページ」列は`https://www.notion.so/{pageId}`
へのリンクにしている。取得件数の上限（200件）に達している場合は、絞り込みを促す注記を表示する。

### アクセス権限（2026-08-17、shirokuma-secレビューBLOCKER対応）

`/audit-log`は`requireRole("master")`で保護している（`users`/`settings/security`と同じ
master限定）。alerts/reports/members等の閲覧系ページはviewer以上に開放しているが、それらは
案件単位・特定粒度の集計情報である一方、監査ログは「誰が・いつ・どのレコードの・どの
フィールドを変更したか」という6DB横断の個人の行動履歴そのもの（氏名・メールアドレス・
電話番号等のPIIの変更前後の値を含む）であり、社内でも閲覧者を絞るべき性質のデータと判断した。
