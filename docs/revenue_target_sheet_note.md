# 事業計画スプレッドシート連携（月次・クオーター目標値）に関する申し送り

営業ステータス管理者向け設定画面（`dashboard/app/(dashboard)/settings/`）から設定する、
月次MRR目標・月次販売数目標の情報源（金沢さんが実際に運用している「事業計画」Google
スプレッドシート）についての実装ノート。

## 設計方針（重要）

目標値そのもの（金額・件数）はNotionにもどこにも複製しない。複製すると「シートを更新した
のにNotion側が古いまま」という二重管理が発生するため（2026-08-13の会話で決定）。持続化する
のは「どのスプレッドシート・どのシート名か」という小さなポインタのみで、値そのものは
`src/reports/revenue_target_sheet.py`の`fetch_mrr_targets`/`fetch_unit_count_targets`/
`fetch_all_targets`で毎回スプレッドシートから直接読みに行く（短時間のTTLキャッシュのみ、
`src/reports/batch.py`の`_cached_fetch_all_targets`参照）。

シートのパース処理そのもの（見出しテキストを基準にした位置特定・fail-closedなエラー処理）は
`src/reports/revenue_target_sheet.py`のモジュールdocstringを参照。実運用スプレッドシートで
検証済みのため変更しないこと。

`initial_fee`（初期費用）目標はこのスプレッドシートに存在しない（金沢さん確認済み）。
このソース経由の`RevenueTarget.initial_fee`は常に`0.0`になる。これは既知・想定通りの制約
であり、バグではない。

## ポインタの永続化先

`src/reports/revenue_target_settings.py`の`RevenueTargetSettingsStore`が、専用のNotion
database（1レコードのみ）へポインタを保存する。設計判断の詳細は同モジュールのdocstring
参照。要点のみ:

- 既存の「データマッピング」DB（IDマッピング専用、`src/sync_engine/notion_id_mapping.py`）は
  再利用しない。無関係な関心事を間借りさせるとスコープが曖昧になるため、新規に専用DBを作る。
- APIトークンはID マッピング専用トークン（`SYNC_ID_MAPPING_NOTION_API_KEY`）を既定で再利用する
  （低ボリュームなアクセスのため、専用トークンをさらに増やすよりも妥当と判断）。

## 【要対応】本番Notionワークスペースへの新規DB作成（未実施）

本変更の実装時点では、この専用Notion databaseは**まだ本番Notionワークスペースに作成していない**。
作成用スクリプト`scripts/setup_revenue_target_settings_db.py`は用意済みだが、どこに配置すべきか
（どの親ページの下に置くか）は本番ワークスペースの構成を把握している人間の判断が必要なため、
実装担当が代わりに判断・実行することを避けた。

このNoteを読んだ人（レビューする側・金沢さん）は、以下の手順で作成を完了させること。

1. Notion側で新規DBの配置先とする親ページを1つ用意する（例:「システム設定」等の適当な場所）。
2. `python scripts/setup_revenue_target_settings_db.py --parent-page-id <親ページID>`を実行する
   （`--dry-run`で事前にペイロードを確認できる。実行には`NOTION_API_KEY`が必要）。
3. 出力されたdatabase_idを環境変数`REVENUE_TARGET_SETTINGS_NOTION_DATABASE_ID`に設定する
   （Vercel環境変数・`config/.env`等）。
4. ダッシュボード設定画面（`/settings`）からスプレッドシートURL・シート名を保存し、
   「✅ N件の月次目標を読み込みました」と表示されることを確認する。

上記が完了するまでは、`src/reports/batch.py`の`_resolve_revenue_targets`が
「ポインタ未設定」として扱い、既存の環境変数（`MONTHLY_TARGET_MRR`等）へ自動的に
フォールバックする（動作は現状のまま変わらない。詳細は`batch.py`のモジュールdocstring参照）。

## 必要な環境変数

| 環境変数 | 説明 |
|---|---|
| `REVENUE_TARGET_SETTINGS_NOTION_DATABASE_ID` | ポインタ専用Notion databaseのID。未設定時はポインタ機能自体が「未構成」として扱われ、環境変数フォールバックのみになる（既定値は無い。上記セットアップ手順参照）。 |
| `REVENUE_TARGET_SETTINGS_NOTION_API_KEY` | ポインタ専用Notion APIトークン。未設定時は`SYNC_ID_MAPPING_NOTION_API_KEY`にフォールバックする。 |
| `REVENUE_TARGET_SHEET_CACHE_TTL_SECONDS` | シート読み取り結果のプロセス内キャッシュTTL（秒）。未設定時は300秒（`src/reports/batch.py`）。 |

上記に加え、シート読み取り自体には既存の`src/document_generation/google_auth.py`が使う
Google認証情報（サービスアカウントJSON等、`docs/spreadsheet_auth_note.md`参照）が必要。
