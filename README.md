# CRM/SFA 統合システム

![CI](https://github.com/kanazawa18-19/crm-sfa-integration/actions/workflows/ci.yml/badge.svg)

Notion をマスターデータベースとし、Google スプレッドシート・kintone・Zoho CRM 間の
Any-to-Any 相互リアルタイム同期、営業分析、自動チーム日報・週報を実現する自社開発基盤。

詳細仕様は [`docs/CRM_SFA_基本詳細仕様書_v2.0.xlsx`](docs/CRM_SFA_基本詳細仕様書_v2.0.xlsx)
（全文テキストは [`docs/spec_full_text_dump.txt`](docs/spec_full_text_dump.txt)）を参照。

## アーキテクチャ概要

- **マスターDB**: Notion（Single Source of Truth、全6DB）
- **閲覧・分析UI**: Google スプレッドシート
- **既存業務DB**: kintone（常時双方向同期）
- **過渡期CRM**: Zoho CRM（`ENABLE_ZOHO=False` で切離し可能）
- **名刺連携**: Eight（片方向 Eight → Notion）
- **同期エンジン**: AWS Lambda / GCP Cloud Functions（Python 3.11）
- ハブ＆スポーク構造。ツール別モジュールは疎結合（`notion_sync` / `spreadsheet_sync` / `kintone_sync` / `zoho_sync`）。

## ディレクトリ構成

```
src/
  db_schema/       Notion 6DB のプロパティ定義・スキーマ定義
  migration/       kintone/Zoho/CSV → Notion への項目マッピング・変換ロジック
  sync_engine/      Any-to-Any 同期・コンフリクト解決・IDマッピング
    webhook_handlers/  各ツールのWebhook受信ハンドラ
  analytics/        営業分析ロジック（接触回数・受注率・コンディション判定・着地予測）
  reports/           日報・週報の生成ロジック
    templates/        配信文面テンプレート（コード非直書き）
  external_data/    Eight名刺連携・国交省/観光庁オープンデータ補記
tests/              各モジュールに対応するユニットテスト
config/             環境変数サンプル・閾値等の設定ファイル
docs/               仕様書・設計ドキュメント
scripts/            Notion DB自動作成などのセットアップスクリプト
gas/                Googleスプレッドシート側のGoogle Apps Script（onEdit変更検知・タブ自動セットアップ）
```

## 開発方針

- スクラッチ開発（iPaaS不使用）。サーバーレスでランニングコスト最小化。
- 各外部ツール連携モジュールは疎結合。`ENABLE_ZOHO=False` のように環境変数だけで
  他システムに影響を与えず切り離せること。
- コンフリクト解決: Notion（マスター）優先。却下データは必ず保全（スプレッドシート同期ログへ退避）。
- 日報・週報のテンプレートはコード非直書き（`src/reports/templates/`）。
- 判定閾値（コンディション判定の14日・1.5倍等）は設定ファイルで外出しし運用調整可能にする。

## セットアップ

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config/.env.example config/.env  # 各種APIキーを設定
```

必要なAPIキー（`config/.env.example` 参照）:
- `NOTION_API_KEY`
- `KINTONE_API_TOKEN` / `KINTONE_DOMAIN`
- `ZOHO_CLIENT_ID` / `ZOHO_CLIENT_SECRET` / `ZOHO_REFRESH_TOKEN`
- `EIGHT_API_KEY`（または定期CSVエクスポート運用）
- `SLACK_WEBHOOK_URL`
- `GOOGLE_SERVICE_ACCOUNT_JSON`

## 開発ロードマップ

仕様書 `09_開発ロードマップ` を参照。Phase 1（DB構築・マッピング確定）→
Phase 2（同期エンジン）→ Phase 3（分析・レポート・外部連携）→
Phase 4（データ移行・総合テスト）→ Phase 5（本番移行）。

■ Phase 2 実装ノート: Notion Webhook実運用向けのプロキシ層（`handler_with_proxy()`）は
実装済み。デプロイ設定自体は別スコープ
（[`docs/notion_webhook_proxy_note.md`](docs/notion_webhook_proxy_note.md) 参照）。

■ Phase 3 実装ノート: メンバー別パフォーマンス評価（週報「営業パフォーマンス分析」）の
「スピード」指標は、既存DBスキーマのみで算出できる簡易代替指標（次回アクション期限遵守率）
で実装している。本来の一次返信時間等の実測にはNotion側への新規プロパティ追加が必要
（[`docs/member_performance_note.md`](docs/member_performance_note.md) 参照）。

■ Phase 2 実装ノート: Googleスプレッドシート連携（`HttpSpreadsheetClient`）は、サービス
アカウントJWTからのトークン取得ではなく`GOOGLE_ACCESS_TOKEN`直接指定による簡略認証で
実装している。本番運用にはサービスアカウントJWTからの自動トークン取得・リフレッシュ処理
への置き換えが必要
（[`docs/spreadsheet_auth_note.md`](docs/spreadsheet_auth_note.md) 参照）。

■ Phase 1 実装ノート: Googleスプレッドシートのタブ構成（`gas/`配下）は、09節ロードマップの
「全5タブ＋同期ログタブ」という記載に対し、6DB業務タブ全部＋分析タブ＋クロスセル対象タブ＋
同期ログタブの計9タブとして実装した。数の不一致は仕様書に明記の無い新規論点として扱っている
（[`docs/spreadsheet_tabs_design.md`](docs/spreadsheet_tabs_design.md) 参照）。変更検知は
GAS（Google Apps Script）の`onEdit`インストーラブルトリガーで実装しており、Python側とは
別ランタイムのため`gas/payloadUtils.test.js`をNode.jsで実行して検証する
（`node --test gas/payloadUtils.test.js`）。

■ Phase 4 実装ノート: kintone CSV → Notion 6DBへの一括インポート（`scripts/migrate_data.py`）
は、取引先マスターDBの「営業ステータス」導出ロジック・リレーションキー列名の推測・USER型
プロパティ未解決・PIIを含む出力ファイルの取り扱い等、04_項目マッピングに明記の無い実装者
判断点が複数ある
（[`docs/migration_pipeline_note.md`](docs/migration_pipeline_note.md) 参照）。

## 保留・要確認事項

仕様書 `10_保留・要確認事項` に10件の未決定論点がある（Q-01〜Q-10）。
特に Q-01（kintone常時同期継続の要否）・Q-09（自動メールログの取得元）は
同期エンジン・分析ロジックの設計に直接影響するため、実装は暫定の想定値
（常時双方向同期を継続 / 自動メールログ取得元は未確定としてインターフェースのみ用意）
で進めている。確定次第、設定ファイルの値を更新すること。

さらに、Google スプレッドシートのタブ数（09節「全5タブ」 vs 実装した9タブ構成）についても
仕様書に明記の無い追加論点（Q-11相当）が発生している
（[`docs/spreadsheet_tabs_design.md`](docs/spreadsheet_tabs_design.md) 参照）。
