"""案件管理DB（Notion、約10,000件）のPostgres複製（読み取り専用ミラー、2026-08-17）。

全社ダッシュボードでの`NotionDataSource.get_projects()`全件取得が実測約100秒かかる問題への
対応。データの正本は引き続きNotionであり、双方向同期パイプライン（Dispatcher/SyncEvent/
IdMappingStore）には一切関与しない（ミラーが古くなっても実害はダッシュボード表示の鮮度
低下のみであり、Notion本体やkintone/Zoho/スプレッドシートへの同期精度・書類自動生成には
影響しない）。

- `db.py`: `ProjectMirror`テーブル(Neon Postgres、スキーマ管理はdashboard側のPrismaに一本化。
  `src/audit_log/db.py`と同じpsycopg直接アクセスのパターン)への読み書き。
- `sync.py`: Notion→ミラーへの同期処理本体（Webhookからの1件更新・バックフィル/夜間
  reconciliationのフル同期）。
"""
