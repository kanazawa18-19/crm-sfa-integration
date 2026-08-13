# CRM/SFA 統合システム

![CI](https://github.com/kanazawa18-19/crm-sfa-integration/actions/workflows/ci.yml/badge.svg)

Notion をマスターデータベースとし、Google スプレッドシート・kintone・Zoho CRM 間の
Any-to-Any 相互リアルタイム同期、営業分析、自動チーム日報・週報を実現する自社開発基盤。

詳細仕様は [`docs/CRM_SFA_基本詳細仕様書_v2.0.xlsx`](docs/CRM_SFA_基本詳細仕様書_v2.0.xlsx)
（全文テキストは [`docs/spec_full_text_dump.txt`](docs/spec_full_text_dump.txt)）を参照。
v2.0確定（2026-08-05）後の変更・実装差分は同ファイルの
`11_変更履歴・実装差分`シートにまとめている。

## アーキテクチャ概要

- **マスターDB**: Notion（Single Source of Truth、全6DB）
- **閲覧・分析UI**: Google スプレッドシート
- **既存業務DB**: kintone（他ツール→kintoneへの一方向書き込みのみ。kintone側での入力は今後行われないため、kintone発の変更同期は実装しない。初期データはNotionへ統合する）
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
- コンフリクト解決: 直近編集優先（`updated_at`が新しい側を採用、同時刻はNotionをタイブレーク優先）。
  却下データは必ず保全（スプレッドシート同期ログへ退避、採用元/却下元ツールも記録）。
  2026-08-12に「Notion常に優先」から変更（金沢さん承認済み。経緯は
  [`docs/zoho_webhook_activation_note.md`](docs/zoho_webhook_activation_note.md) 参照）。
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

■ Phase 2 実装ノート: Googleスプレッドシート連携（`HttpSpreadsheetClient`）は、
サービスアカウントJWTからの自動トークン取得・リフレッシュ処理
（`src/document_generation/google_auth.py`の`get_google_access_token()`）で
実装済み。`GOOGLE_ACCESS_TOKEN`直接指定はローカル動作確認向けのフォールバックとして
残している
（[`docs/spreadsheet_auth_note.md`](docs/spreadsheet_auth_note.md) 参照）。

■ Phase 1 実装ノート: Googleスプレッドシートのタブ構成（`gas/`配下）は、09節ロードマップの
「全5タブ＋同期ログタブ」という記載に対し、6DB業務タブ全部＋分析タブ＋クロスセル対象タブ＋
同期ログタブの計9タブとして実装した。数の不一致は仕様書に明記の無い新規論点として扱っている
（[`docs/spreadsheet_tabs_design.md`](docs/spreadsheet_tabs_design.md) 参照）。変更検知は
GAS（Google Apps Script）の`onEdit`インストーラブルトリガーで実装しており、Python側とは
別ランタイムのため`gas/payloadUtils.test.js`をNode.jsで実行して検証する
（`node --test gas/payloadUtils.test.js`）。

■ Phase 2 実装ノート（2026-08-13）: 上記Phase 1ノートの9タブ構成（GAS `setupTemplate.js`
経由）は設計のみで、本番スプレッドシートには未適用だった。同期エンジンが実際に読み書きする
6業務タブ＋同期ログタブの計7タブ（分析・クロスセル対象リストの2タブは対応するPython側の
書き込みコードが存在しないため対象外）は、Sheets APIで直接作成・ヘッダー設定し、サービス
アカウントへの共有権限付与とあわせて2026-08-13に動作確認済み（Notion→スプレッドシート
方向）。**GAS側の`onEdit`トリガーが本番スプレッドシートに実際に設置されているかは未確認**
であり、スプレッドシート側での直接編集が他ツールへ反映される保証はまだ無い。

■ Phase 4 実装ノート: kintone CSV → Notion 6DBへの一括インポート（`scripts/migrate_data.py`）
は、取引先マスターDBの「営業ステータス」導出ロジック・リレーションキー列名の推測・USER型
プロパティ未解決・PIIを含む出力ファイルの取り扱い等、04_項目マッピングに明記の無い実装者
判断点が複数ある
（[`docs/migration_pipeline_note.md`](docs/migration_pipeline_note.md) 参照）。

■ 重要な前提変更（2026-08-06）: `src/db_schema/` は当初「Notionに新規6DBをゼロから作る」
前提で設計していたが、実際には既存の稼働中Notionワークスペースに4DB（取引先マスター/
チェーン/案件管理/アクション履歴）が既に存在し、独自のプロパティ構成（多数のrollup/formula
/button等の読み取り専用プロパティを含む）を持っていることが判明した。これに合わせて
`src/db_schema/`・`sync_engine`のNotion変換層・`analytics`/`reports`の営業ステータス判定
ロジックを全面的に実データ構造へ書き直した。営業ステータス11値・確度A〜D（S/A/B/Cではない）
等、仕様書の想定と異なる点が多数ある。`scripts/setup_notion_databases.py`は「新規2DB
（連絡先・サービス商品）へのプロパティ追加専用」に役割を変更し、既存4DBには一切変更を
加えない設計にしている（Notion APIの`dual_property`が参照先DBに副作用を及ぼす問題を
レビューで検出・修正済み）。

■ Phase 2 実装ノート（2026-08-12/13）: IDマッピングストア（Notion主キー⇔kintone/Zoho/
スプレッドシート外部ID）は、Vercelサーバーレスの`/tmp`が揮発する制約からSQLite常設運用が
できないため、GCP/AWSへの正式DB移行までの暫定ブリッジとしてNotion裏付けの
`NotionIdMappingStore`を導入した
（[`docs/id_mapping_persistence_note.md`](docs/id_mapping_persistence_note.md) 参照）。

■ Phase 2 実装ノート（2026-08-12/13）: Zoho CRM⇔Notionのリアルタイム双方向Webhook同期は、
案件（Deals）のみから全6モジュール（チェーン/アクション/取引先マスター/連絡先/商品）へ
拡張済み。コンフリクト解決も「Notion常に優先」から「直近編集優先」へ変更した
（[`docs/zoho_webhook_activation_note.md`](docs/zoho_webhook_activation_note.md) 参照）。
このうち取引先マスター（client_master）は、移行時にkintone由来ページへZohoレコードを
マージする特殊な設計のため、当初IDマッピングストアにkintone⇔Zoho対応が一切記録されて
いない欠落が2026-08-13に発覚し、32,059件分を再構築して解消した
（[`docs/client_master_id_mapping_note.md`](docs/client_master_id_mapping_note.md) 参照）。

■ Phase 3 実装ノート（2026-08-12）: マネージャー通知（失注/失注候補/停滞案件/契約成立の
一覧、ダッシュボード`/alerts`）を追加した。バックエンドAPIの既知の制約は
[`docs/dashboard_note.md`](docs/dashboard_note.md) 参照。

■ Phase 3 実装ノート（2026-08-13）: 自社の決算期は暦年（1月始まり）ではなく**期初12月・
期末11月**であることが判明したため、`src/analytics/fiscal_calendar.py`に
`FISCAL_YEAR_START_MONTH`という名前付き定数として一本化した。週報「当クオーター」の
判定（`src/reports/batch.py`の`run_weekly_report`、旧実装は暦四半期1-3月/4-6月/7-9月/
10-12月で計算しておりバグだった）と、ダッシュボードのクオーター/半期/通期着地予測
（`src/api/dashboard_service.py`の`build_dashboard_summary()`、旧実装は日付での絞り込みを
一切行わず全案件を1つのクオーター予測に流し込んでいた）の両方がこのモジュールを経由する。
着地予測の期間への案件の帰属判定は、契約済案件は「契約日 / 予想契約日」の実際の契約日、
進行中案件は同じプロパティに営業担当が入力した予想契約日を使う（1つのNotionプロパティを
案件のステータスに応じて意味を変えて読む非自明な仕様のため注意）。この予想契約日が
未入力の進行中案件は、曖昧に含めたり除外したりせず3期間いずれの予測にも計上せず、
`unscheduled_active_count`として件数のみ別途返す。

■ 未対応・保留中の機能: Eight連携（名刺データ自動登録）、商談手当計算、外部データ連携
（国交省/観光庁オープンデータ）は、いずれも詳細確認待ちで実装未着手。kintoneのCSV
エクスポートが揃うまで、`src/migration/`パッケージの実データ完全対応（顧客種別・契約進捗
状況・アクション内容のkintone値↔Notion値マッピング）も保留中。Zoho CRMとNotionの
データマージ（client_master含む）は完了済み（上記2026-08-12/13ノート参照）。

## 保留・要確認事項

仕様書 `10_保留・要確認事項` に10件の未決定論点がある（Q-01〜Q-10）。

**Q-01（kintone常時同期継続の要否）は確定済み**: kintone側での入力は今後行われない。
常時双方向同期の対象ではなく、「他ツール（Notion/スプレッドシート/Zoho）→kintoneへの
一方向書き込み」のみを行う。kintone発のWebhookイベントは想定しない
（`src/sync_engine/webhook_handlers/kintone_webhook.py`のdocstring参照）。
初期データ（取引先・案件・アクション）はNotionへ一括統合する
（[`docs/migration_pipeline_note.md`](docs/migration_pipeline_note.md) 参照、kintone CSV到着待ち）。

Q-09（自動メールログの取得元）は同期エンジン・分析ロジックの設計に直接影響するため、
実装は暫定の想定値（未確定としてインターフェースのみ用意）で進めている。確定次第、
設定ファイルの値を更新すること。

さらに、Google スプレッドシートのタブ数（09節「全5タブ」 vs 実装した9タブ構成）についても
仕様書に明記の無い追加論点（Q-11相当）が発生している
（[`docs/spreadsheet_tabs_design.md`](docs/spreadsheet_tabs_design.md) 参照）。
