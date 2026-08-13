"use strict";

/**
 * スプレッドシートの全タブ構成・ヘッダー列定義（T-02 テンプレート作成）。
 *
 * タブ構成を決めた理由の詳細は docs/spreadsheet_tabs_design.md を参照。
 *
 * 6DB業務タブの列は src/db_schema/*.py の各 DatabaseSchema.properties_synced_to(Tool.SPREADSHEET)
 * （sync_scope が ALL_TOOLS または SPREADSHEET_ONLY のプロパティ）と一致させている。
 * GASはPython側のdb_schemaを実行時にimportできないため、この定義は手動でPython側と
 * 同期させること。Python側でプロパティを追加・変更した場合は、対応するタブのheadersにも
 * 反映すること。
 *
 * 2026-08-13追記: 本ファイルは2026-08-06時点のdb_schemaを元に作成されたが、その後の
 * db_schema側の変更（プロパティ名の変更・追加）が反映されないまま長期間ドリフトしていた
 * ことが判明した（本番スプレッドシートにこのGASコードが一度も適用されていなかったため
 * 実害は無かったが、次回コード適用時に古いヘッダーで上書きされるリスクがあった）。
 * `python -c "from src.db_schema.registry import ALL_SCHEMAS; from src.db_schema.base import Tool; [print(s.spreadsheet_sheet_name, [p.name for p in s.properties_synced_to(Tool.SPREADSHEET)]) for s in ALL_SCHEMAS]"`
 * の出力に合わせて全面的に更新した（docs/client_master_id_mapping_note.md参照）。
 *
 * 各業務タブ末尾の DELETE_FLAG_COLUMN は、src/sync_engine/sync_targets/spreadsheet_sync.py の
 * _DELETE_FLAG_COLUMN（論理削除の実装）に対応する、Notion側DBスキーマ上には存在しない
 * スプレッドシート固有の列。
 *
 * 各タブの autoColumns は、対応する src/db_schema/*.py 上で
 * RequirementLevel.AUTO（システムが自動算出・自動投入する項目。PropertyDefinition.is_auto）
 * と定義されている列の一覧（headers のサブセット）。setupTemplate.js の setupTab_ が
 * これらの列に警告色・注記コメントを付け、営業担当が誤って手入力しないよう視覚的に
 * 注意喚起する。2026-08-13時点、スプレッドシートに同期される範囲でAUTO指定のプロパティは
 * 存在しないため全タブ空配列。
 */

var DELETE_FLAG_COLUMN = "削除フラグ";

// 02_DB構成一覧の並び順（①〜⑥）を踏襲する（src/db_schema/registry.py の ALL_SCHEMAS と同順）。
var BUSINESS_TABS = [
  {
    sheetName: "取引先マスター",
    dbKey: "client_master",
    headers: [
      "取引先名",
      "顧客種別",
      "都道府県",
      "郵便番号",
      "住所",
      "TEL",
      "FAX",
      "決算",
      "予算組の時期",
      "日付",
      "備考",
      "チェーン",
      "サービス・商品",
      "【営業部】案件管理DB",
      "【営業部・パーソネル】アクション履歴DB",
      DELETE_FLAG_COLUMN,
    ],
    autoColumns: [],
  },
  {
    sheetName: "チェーン",
    dbKey: "chain",
    headers: [
      "グループ名",
      "アプローチ状況",
      "施設数",
      "本社",
      "本社所在地",
      "運営会社",
      "電話",
      "URL",
      "自動チェックインURL",
      "自動チェックイン",
      "決裁",
      "メイリー",
      "リピッテ",
      "ホテラボ",
      "オルト",
      "三密",
      "その他ブランド",
      "その他",
      "未導入店舗へのアプローチ",
      "メモ",
      "最終アプローチ日",
      "担当",
      "👨‍👩‍👧‍👦 取引先マスター",
      "案件管理",
      "アクション履歴",
      "連絡先",
      "サービス・商品",
      DELETE_FLAG_COLUMN,
    ],
    autoColumns: [],
  },
  {
    sheetName: "連絡先",
    dbKey: "contact",
    headers: [
      "名前",
      "取引先マスター",
      "案件管理",
      "アクション履歴",
      "チェーン",
      "部署",
      "役職",
      "メールアドレス",
      "携帯番号",
      "直通TEL",
      DELETE_FLAG_COLUMN,
    ],
    autoColumns: [],
  },
  {
    sheetName: "案件管理",
    dbKey: "project",
    headers: [
      "案件名",
      "営業ステータス",
      "確度",
      "ファーストタッチ",
      "提案サービス",
      "サイトコントローラー",
      "例外スイッチ（途中解約・複数サービス提案など）",
      "かつやさん",
      "問合せ",
      "担当者名",
      "決裁者名",
      "ネックポイント",
      "失注理由",
      "次回アクション",
      "メモ",
      "テキスト",
      "メールアドレス",
      "電話番号",
      "初期費用",
      "月額費用",
      "【例外】粗利",
      "サービス数（施設数）",
      "ショット",
      "契約日 / 予想契約日",
      "次回アクション日",
      "失注日",
      "再アプローチ日",
      "担当メンバー",
      "取引先マスター",
      "チェーン",
      "アクション履歴",
      "連絡先",
      "サービス・商品",
      DELETE_FLAG_COLUMN,
    ],
    autoColumns: [],
  },
  {
    sheetName: "サービス・商品",
    dbKey: "product",
    headers: [
      "名前",
      "課金形態",
      "標準初期費用",
      "標準月額費用",
      "案件管理",
      "取引先マスター",
      "チェーン",
      DELETE_FLAG_COLUMN,
    ],
    autoColumns: [],
  },
  {
    sheetName: "アクション管理",
    dbKey: "action",
    headers: [
      "商談回数・電話回数・メール回数（何回目）",
      "アクション種別",
      "アクション日",
      "導入フローとスケジュール",
      "履歴メモ",
      "議事録・録画リンク",
      "先方担当者",
      "案件名",
      "👯‍♀️ チェーンリスト",
      "👨‍👩‍👧‍👦 取引先マスター",
      "連絡先",
      DELETE_FLAG_COLUMN,
    ],
    autoColumns: [],
  },
];

// 06_営業分析ロジックの出力先タブ。夜間/週次バッチ（09_開発ロードマップ T-08）が書き込む想定の
// テンプレート（空のヘッダー行）のみ用意する。列構成は src/analytics/win_rate.py・win_pattern.py
// の出力項目に合わせている。GAS onEditの同期対象（BUSINESS_SHEET_NAMES）には含めない
// （バッチ生成物であり、双方向Any-to-Any同期の対象ではないため）。
// 2026-08-13時点、これらのタブへ実際に書き込むPython側の実装は存在しない（分析結果は
// ダッシュボードAPI経由で提供している）。タブの用意のみ行い、書き込み先としての採用は
// 将来判断とする。
var ANALYSIS_TABS = [
  {
    sheetName: "分析",
    headers: ["接触回数（段階）", "段階別受注率", "累積受注率（N回以内）", "算出日時"],
  },
  {
    sheetName: "クロスセル対象リスト",
    headers: ["取引先ID", "取引先名", "契約中サービス", "未提案サービス（クロスセル対象）", "算出日時"],
  },
];

// 05_同期・競合制御「データ退避」。列構成は
// src/sync_engine/sync_targets/spreadsheet_sync.py の append_conflict_log と一致させる
// （2026-08-12の「採用元ツール」列追加を反映）。
var SYNC_LOG_TAB = {
  sheetName: "同期ログ",
  headers: ["対象ID", "項目名", "採用値", "採用元ツール", "却下値", "却下元ツール", "発生日時"],
};

var ALL_TABS = BUSINESS_TABS.concat(ANALYSIS_TABS, [SYNC_LOG_TAB]);

var BUSINESS_SHEET_NAMES = BUSINESS_TABS.map(function (tab) {
  return tab.sheetName;
});

// Node.js（`node --test gas/`）用のエクスポート。GASランタイムには module が存在しないため
// 条件付きにする（GAS側では全.gs/.jsファイルが1つのグローバルスコープに連結されるため、
// この分岐は不要かつ到達しない）。
if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    DELETE_FLAG_COLUMN: DELETE_FLAG_COLUMN,
    BUSINESS_TABS: BUSINESS_TABS,
    ANALYSIS_TABS: ANALYSIS_TABS,
    SYNC_LOG_TAB: SYNC_LOG_TAB,
    ALL_TABS: ALL_TABS,
    BUSINESS_SHEET_NAMES: BUSINESS_SHEET_NAMES,
  };
}
