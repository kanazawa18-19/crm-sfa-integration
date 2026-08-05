# Notion Webhook 実運用に関する申し送り（Phase 2 実装ノート）

`src/sync_engine/webhook_handlers/notion_webhook.py` の `handler()` は、
「ページの変更後プロパティをフルに含むペイロードが届く」ことを前提に実装されている。

しかし実際の Notion API Webhooks は **変更されたプロパティIDのみ** を通知し、
ページ全体（他のプロパティの現在値）は含まない。そのため、本番投入前に以下のプロキシ層を
Webhook 受信〜`handler()` 呼び出しの間に必ず追加実装する必要がある。

1. Notion Webhook を受信する
2. 通知された `page_id` を使い、Notion API `GET /v1/pages/{page_id}` でページ全体を再取得する
3. `fetch_and_normalize_notion_page(page_id, notion_client)`
   （`src/sync_engine/webhook_handlers/notion_webhook.py` に実装済み）で、
   本モジュールが期待する `{"page_id", "database_id", "last_edited_time", "properties"}` 形式へ整形する
4. 整形済みペイロードを `handler()` に渡す

現時点では `fetch_and_normalize_notion_page()` の最小実装のみを用意しており、
実際の Notion API クライアント（HTTP通信部分）はスコープ外。09_開発ロードマップの
T-05/T-06（同期エンジン実装）の一環として、本プロキシ層の実装を追加タスクとして計上すること。
