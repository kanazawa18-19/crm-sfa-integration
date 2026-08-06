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
 * 各業務タブ末尾の DELETE_FLAG_COLUMN は、src/sync_engine/sync_targets/spreadsheet_sync.py の
 * _DELETE_FLAG_COLUMN（論理削除の実装）に対応する、Notion側DBスキーマ上には存在しない
 * スプレッドシート固有の列。
 *
 * 各タブの autoColumns は、対応する src/db_schema/*.py 上で
 * RequirementLevel.AUTO（システムが自動算出・自動投入する項目。PropertyDefinition.is_auto）
 * と定義されている列の一覧（headers のサブセット）。setupTemplate.js の setupTab_ が
 * これらの列に警告色・注記コメントを付け、営業担当が誤って手入力しないよう視覚的に
 * 注意喚起する。
 */

var DELETE_FLAG_COLUMN = "削除フラグ";

// 02_DB構成一覧の並び順（①〜⑥）を踏襲する（src/db_schema/registry.py の ALL_SCHEMAS と同順）。
var BUSINESS_TABS = [
  {
    sheetName: "取引先マスター",
    dbKey: "client_master",
    headers: [
      "取引先ID",
      "取引先名",
      "顧客種別",
      "営業ステータス",
      "チェーン",
      "郵便番号",
      "都道府県",
      "住所",
      "TEL",
      "FAX",
      "メールアドレス",
      "WEBサイト",
      "エリア属性データ",
      "エリアポテンシャルスコア",
      DELETE_FLAG_COLUMN,
    ],
    // エリアポテンシャルスコア: エリア属性データから自動算出（client_master.py）。
    autoColumns: ["エリアポテンシャルスコア"],
  },
  {
    sheetName: "チェーン",
    dbKey: "chain",
    headers: [
      "チェーンID",
      "チェーン名",
      "グループ名",
      "運営会社",
      "施設数",
      "本社",
      "本社所在地",
      "アプローチ状況",
      "取引先マスター",
      DELETE_FLAG_COLUMN,
    ],
    autoColumns: [],
  },
  {
    sheetName: "連絡先",
    dbKey: "contact",
    headers: [
      "連絡先ID",
      "氏名",
      "取引先マスター",
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
      "案件ID",
      "案件名",
      "取引先マスター",
      "営業ステータス",
      "提案サービス",
      "初期費用（イニシャル）",
      "月額費用（ランニング）",
      "粗利",
      "確度",
      "契約日",
      "予想契約日",
      "総接触回数",
      "最終アクション日",
      "コンディション判定",
      "担当メンバー",
      "次回アクション日",
      "失注理由",
      "失注ナレッジ",
      DELETE_FLAG_COLUMN,
    ],
    // 総接触回数・最終アクション日・コンディション判定: アクションDBから自動集計/判定（project.py）。
    autoColumns: ["総接触回数", "最終アクション日", "コンディション判定"],
  },
  {
    sheetName: "サービス・商品",
    dbKey: "product",
    headers: [
      "サービスID",
      "サービス名",
      "課金形態",
      "標準初期費用",
      "標準月額費用",
      DELETE_FLAG_COLUMN,
    ],
    autoColumns: [],
  },
  {
    sheetName: "アクション管理",
    dbKey: "action",
    headers: [
      "営業部アクションID",
      "アクション名",
      "アクション種別",
      "アクション日",
      "商談回数（何回目）",
      "担当営業",
      "案件管理",
      "取引先マスター",
      "先方担当者",
      "履歴メモ",
      DELETE_FLAG_COLUMN,
    ],
    // 商談回数（何回目）: 自動採番（action.py）。
    autoColumns: ["商談回数（何回目）"],
  },
];

// 06_営業分析ロジックの出力先タブ。夜間/週次バッチ（09_開発ロードマップ T-08）が書き込む想定の
// テンプレート（空のヘッダー行）のみ用意する。列構成は src/analytics/win_rate.py・win_pattern.py
// の出力項目に合わせている。GAS onEditの同期対象（BUSINESS_SHEET_NAMES）には含めない
// （バッチ生成物であり、双方向Any-to-Any同期の対象ではないため）。
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
// src/sync_engine/sync_targets/spreadsheet_sync.py の append_conflict_log と一致させる。
var SYNC_LOG_TAB = {
  sheetName: "同期ログ",
  headers: ["対象ID", "項目名", "採用値", "却下値", "却下元ツール", "発生日時"],
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
