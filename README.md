# CRM/SFA 統合システム

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

## 保留・要確認事項

仕様書 `10_保留・要確認事項` に10件の未決定論点がある（Q-01〜Q-10）。
特に Q-01（kintone常時同期継続の要否）・Q-09（自動メールログの取得元）は
同期エンジン・分析ロジックの設計に直接影響するため、実装は暫定の想定値
（常時双方向同期を継続 / 自動メールログ取得元は未確定としてインターフェースのみ用意）
で進めている。確定次第、設定ファイルの値を更新すること。
