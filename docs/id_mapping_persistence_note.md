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

## 暫定ブリッジ: Notion裏付けの`NotionIdMappingStore`（2026-08-12追加）

上記1〜3（GCP/AWS等の永続DB契約・実装差し替え）が完了するまでの数週間、`SQLite`（`/tmp`、
非永続）の代わりに使える暫定ブリッジとして、`src/sync_engine/notion_id_mapping.py` の
`NotionIdMappingStore` を追加した。Notionは（Vercelの`/tmp`と異なり）永続的なストレージの
ため、この期間中の安全網として使う想定である。

**本ノートの「Webhook購読登録の前に必ず対応すること」自体は変わらない**。
`NotionIdMappingStore` はあくまで「実DBが用意できるまでの間、`/tmp`よりはマシな暫定策」で
あり、下記の既知の制約（重複外部ID検知のレース窓）があるため、恒久対応として位置づけては
いない。

### 何であるか・なぜNotionか

- `IdMappingStore` インターフェースのNotion API裏付け実装。既存の6DB（取引先マスタ等）とは
  別の、ID マッピング専用のNotion database（`3b9d8ea8-d4f3-8059-8b04-ee5308d2cbf0`、
  タイトル「データマッピング」）にレコードを1件＝1マッピングとして保持する。
- 実データ移行（`scripts/migrate_data.py`）で使っているのと同じNotion APIを流用でき、
  追加のインフラ契約なしに即座に永続化できるため、GCP/AWS契約までのつなぎとして採用した。

### 2つのリスク低減策

1. **専用Notion database**: 実データ6DBとは別のdatabaseを使うことで、ID マッピングの
   読み書きが実データ同期のNotion APIレート制限を消費しないようにする。
2. **専用Notion APIトークン**: コンテンツ同期用の`NOTION_API_KEY`とは別の、ID マッピング
   専用トークン（`SYNC_ID_MAPPING_NOTION_API_KEY`）を使う。同一トークンだと、大量の
   コンテンツ同期書き込みとID マッピングの読み書きが同じレート制限バケツを奪い合う。

### 既知の制約: 重複外部ID検知のレース窓

`SQLiteIdMappingStore` はUNIQUE INDEXによるDBレベルの一意制約を持ち、事前チェック
（`_assert_no_duplicate_external_id`）をすり抜けた場合でも`sqlite3.IntegrityError`で
最終的に検知できる（belt-and-suspenders）。Notion側にはDBレベルの一意制約が無いため、
`NotionIdMappingStore`の重複外部ID検知は`upsert()`内の事前チェック（クエリで既存レコードを
検索する）のみが唯一の防御線となる。

したがって、ほぼ同時に2つのWebhookが同じ外部ID（kintone_id/zoho_id/spreadsheet_row）を持つ
異なるnotion_keyへupsertした場合、両方の事前チェックが「重複なし」と判定してしまい、
`DuplicateExternalIdError`を検知できずに両方とも書き込まれてしまうレース窓が存在する。
分散ロック等によるこの窓の解消は本実装のスコープ外であり、対応しない（実データ移行済み
レコードへの反映が主用途である現状の運用では、同一外部IDへの同時書き込みは稀という前提）。

### 新たに生じる相関障害: Notion障害・レート制限枯渇が全ツールの同期を止めうる

上記の「専用database」「専用トークン」はいずれもレート制限の**枠**（quota）を実データ同期と
奪い合わないための対策であり、Notion API自体の**可用性**（availability）とは別問題である。

`Dispatcher._resolve_mapping()`（`src/sync_engine/dispatcher.py`）は、Webhookの送信元ツールが
kintone/Zoho/スプレッドシートのいずれであっても、IDマッピングストア（＝Notionバックエンド
選択時は`NotionIdMappingStore`）を呼び出してレコードを特定する。そのため、Notion API側が
障害中またはレート制限を使い切っている（`INTERACTIVE_MAX_RATE_LIMIT_RETRIES`を使い切って
`NotionIdMappingStoreApiError`を送出する状態）と、Notion発のイベントだけでなく
kintone/Zoho/スプレッドシート発のイベントの同期処理まで丸ごと止まる。

これは`SQLiteIdMappingStore`使用時には存在しなかった新しい相関障害モードである
（SQLiteはローカルファイルのため、Notion APIの可用性に依存しない）。Notionバックエンドを
選択する場合はこの点を認識しておくこと。

### 環境変数

| 環境変数 | 説明 |
|---|---|
| `SYNC_ID_MAPPING_BACKEND` | `"sqlite"`（既定）または`"notion"`。`"notion"`で`NotionIdMappingStore`を使う。 |
| `SYNC_ID_MAPPING_NOTION_DATABASE_ID` | ID マッピング専用Notion databaseのID。未設定時は`3b9d8ea8-d4f3-8059-8b04-ee5308d2cbf0`（「データマッピング」）。 |
| `SYNC_ID_MAPPING_NOTION_API_KEY` | ID マッピング専用のNotion APIトークン（コンテンツ同期用`NOTION_API_KEY`とは別）。未設定時は`ValueError`。 |
