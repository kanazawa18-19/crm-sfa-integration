"""データ監査ログ(2026-08-17、金沢さんからの「最終編集者・作成者・編集内容を記録したい」
要望対応)。

Notionへの全書き込み(ページ作成・プロパティ更新)が最終的に集約される共通層
（`HttpNotionClient.create_page`/`update_page`、`src/sync_engine/clients/notion_client.py`）に
フックして自動記録する。呼び出し元（kintone Webhook・Zoho Webhook・Gmail同期・
会議同期・一括移行等）ごとに個別のログ呼び出しを仕込む方式は書き漏れリスクが高いため
避け、共通層1箇所の改修に閉じている。

- `actor_context.py`: 「今処理している書き込みがどの経路から来たか」を表す文字列
  （`actorSource`/`actorLabel`）を、各呼び出し元のエントリポイントから
  `contextvars`経由で伝える。`HttpNotionClient`は単一インスタンスが複数の経路
  （kintone/Zoho双方向同期など）から共有されうるため、クライアントのコンストラクタ引数
  ではなく呼び出し時のコンテキストとして持たせる設計。
- `db.py`: `AuditLog`テーブル(Neon Postgres、スキーマ管理はdashboard側のPrismaに一本化。
  `src/gmail_sync/db.py`と同じpsycopg直接アクセスのパターン)への書き込み。
- `recorder.py`: `HttpNotionClient`から呼ばれる、差分抽出・値の単純化・DB書き込みの本体。

対象範囲の制約: Notion自体にフィールド単位の変更履歴を返すAPIが無いため、このバックエンド
（このコードベース）を経由したNotion書き込みのみが対象。Notion管理画面から人間が直接編集
した変更はこのコードを経由しないため技術的に捕捉できない。
"""
