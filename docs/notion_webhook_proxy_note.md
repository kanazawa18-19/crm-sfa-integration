# Notion Webhook 実運用に関する申し送り（Phase 2 実装ノート）

`src/sync_engine/webhook_handlers/notion_webhook.py` の `handler()` は、
「ページの変更後プロパティをフルに含むペイロードが届く」ことを前提に実装されている。

しかし実際の Notion API Webhooks は **変更されたプロパティIDのみ** を通知し、
ページ全体（他のプロパティの現在値）は含まない。そのため、以下のプロキシ層を
Webhook 受信〜`handler()` 相当の処理呼び出しの間に挟む必要がある。

1. Notion Webhook を受信する（軽量ペイロード。`entity.id` に `page_id` を含む）
2. 通知された `page_id` を使い、Notion API `GET /v1/pages/{page_id}` でページ全体を再取得する
3. `fetch_and_normalize_notion_page(page_id, notion_client)`
   （`src/sync_engine/webhook_handlers/notion_webhook.py` に実装済み）で、
   本モジュールが期待する `{"page_id", "database_id", "last_edited_time", "properties"}` 形式へ整形する
4. 整形済みペイロードを `handler()` 相当の処理（`notion_payload_to_sync_event` -> `SyncEvent` -> dispatch）へ渡す

## 実装状況（実装済み）

上記1〜4を一体化したエントリポイントとして `handler_with_proxy(event, context, *,
notion_client, dispatcher=None)`（`src/sync_engine/webhook_handlers/notion_webhook.py`）を実装済み。
実運用ではこの `handler_with_proxy()` を使う（`handler()` は整形済みペイロード前提のまま
テスト・段階的移行用に残している）。

- Notion API へページ全体を再取得するクライアントには、`HttpNotionClient.get_raw_page(page_id)`
  （`src/sync_engine/clients/notion_client.py`）を利用する。既存の `get_page()`（`NotionSyncTarget`
  向けにプロパティを内部値のフラット辞書へ変換して返す）とは別に、Notion API生レスポンス
  （`id`/`parent`/`last_edited_time`/`properties` を含む）をそのまま返すメソッドとして追加した。
  `notion_webhook.py` の `NotionPageClient` Protocol のメソッド名も `get_raw_page` としており、
  `fetch_and_normalize_notion_page()` はこの名前でクライアントを呼び出す（`HttpNotionClient`が
  実際にProtocolを満たすことは `tests/sync_engine/webhook_handlers/test_notion_webhook.py` の
  `requests_mock` を用いた統合テストで検証している）。
- 軽量ペイロードの必須フィールド（`entity.id` 等）が欠けている場合は400を返す。
- Notion API呼び出し（`get_raw_page`）が失敗した場合、ページ削除済み等で404が返った場合は
  Notion Webhooksの再送による無駄な再送ループを避けるため200＋`{"skipped": "page_not_found"}`
  で応答する（削除イベント自体をSyncEvent/Dispatcher側へ伝播する処理は未実装のため範囲外）。
  404以外のAPIエラー・予期しない例外の場合は500を返し、詳細はログにのみ出力する。
- 署名検証（`verify_webhook_secret`）は既存の `handler()` と同様に行う。

## 残っているスコープ外事項

- 実際のサーバーレスデプロイ設定（SAM/Serverless Framework等でのAPI Gateway/Lambda登録、
  Notion Webhooks Subscription登録・Verification Token検証手順）自体は本実装のスコープ外。
- `handler_with_proxy()` に渡す `notion_client` は呼び出し側（デプロイ設定側）で
  `HttpNotionClient` を組み立てて注入すること（本モジュールはデフォルトのHTTPクライアントを
  内部で構築しない。テスト容易性・DB単位でのインスタンス化方針との整合のため）。
