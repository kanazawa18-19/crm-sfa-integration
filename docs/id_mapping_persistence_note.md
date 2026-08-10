# IDマッピングストアの永続化に関する申し送り（Webhook購読登録前に必読）

`src/sync_engine/id_mapping.py` の `IdMappingStore` は本番想定として DynamoDB / Firestore
等の永続ストアを想定した抽象インターフェースだが、現時点では `SQLiteIdMappingStore`
（ローカルファイルベースのSQLite実装）のみが実装済みである。

`src/sync_engine/production_wiring.py`（Webhook受信エンドポイント `src/api/app.py`
`/api/webhooks/*` が使う本番用配線）は、このSQLite実装をそのまま使っている
（`build_id_mapping_store()`、デフォルト置き場は `SYNC_ID_MAPPING_DB_PATH` 環境変数、
未設定時は `/tmp/sync_id_mapping.db`）。

## 【危険】Vercel Python Functionsでの非永続化リスク

Vercelのサーバーレス実行環境（Python Functions）では、書き込み可能なのは `/tmp` 配下のみ
であり、かつ `/tmp` はコンテナのコールドスタートのたびに消える（実行インスタンス間で
永続化される保証が無い）。

この状態のまま実際にkintone/Zoho/Notion/スプレッドシート側でWebhook購読登録を行うと、
以下の事故が起こりうる。

1. コールドスタートのたびにIDマッピングDBが空になる。
2. `Dispatcher._resolve_mapping()`（`src/sync_engine/dispatcher.py`）が対象レコードの
   `IdMapping` を見つけられず、`unknown_record` として **全ての同期イベントをスキップする**。
3. これは新規レコードだけでなく、`scripts/migrate_data.py` で既に移行済みのレコードの
   同期も含めて丸ごとスキップされることを意味する（IDマッピング自体が失われるため）。

## 現在の安全網（本ノート時点での対応）

上記の問題自体（SQLite→永続ストアへの置き換え）は本ノート作成時点では未対応であり、
規模の大きい別作業として先送りしている（元々「今夜開始した本番データ移行が完了するまで
Webhook購読登録自体を行わない」方針のため、実害は発生していない）。

安全網として、`production_wiring.build_id_mapping_store()` は、解決された永続化先が
`/tmp` 配下または `:memory:`（インメモリ、さらに非永続）である場合、プロセス起動後
最初の1回だけ `logger.warning` で目立つ警告ログを出す
（`_warn_if_id_mapping_store_not_persistent()`）。

## Webhook購読登録の前に必ず対応すること

1. `IdMappingStore` の永続ストア実装を追加する（例: Vercel Marketplace経由のNeon Postgres、
   DynamoDB等）。
2. `production_wiring.build_id_mapping_store()` を新実装へ差し替える
   （`SYNC_ID_MAPPING_DB_PATH` のようなファイルパスベースの設定ではなく、接続先DB情報を
   環境変数から読む形に変更する）。
3. 上記対応が完了し、実際にコンテナ再起動後もIDマッピングが失われないことを確認して
   初めて、kintone/Zoho/Notion/スプレッドシート側でのWebhook購読登録を行うこと。
