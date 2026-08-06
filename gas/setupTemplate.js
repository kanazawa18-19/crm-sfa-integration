"use strict";

/**
 * scripts/setup_notion_databases.py のスプレッドシート版（T-02: 全タブのテンプレート作成）。
 *
 * 新規（または既存）のGoogleスプレッドシートへ、tabDefinitions.js に定義したタブ構成・
 * ヘッダー行を自動セットアップし、GAS onEditのインストーラブルトリガーを登録する。
 * タブ構成を決めた理由の詳細は docs/spreadsheet_tabs_design.md を参照。
 *
 * 実行方法: Apps Scriptエディタの関数選択で setupAll を選び実行する（初回のみ要承認）。
 */

function setupAll() {
  var ss = getTargetSpreadsheet_();
  ALL_TABS.forEach(function (tab) {
    setupTab_(ss, tab);
  });
  installEditTrigger_(ss);
}

function getTargetSpreadsheet_() {
  // スタンドアロンスクリプトとして運用する前提でスクリプトプロパティのSPREADSHEET_IDを
  // 優先し、コンテナバインドスクリプトとして使う場合のみアクティブなスプレッドシートに
  // フォールバックする。
  var id = PropertiesService.getScriptProperties().getProperty("SPREADSHEET_ID");
  return id ? SpreadsheetApp.openById(id) : SpreadsheetApp.getActiveSpreadsheet();
}

var AUTO_COLUMN_BACKGROUND = "#fff2cc"; // 警告色（薄黄色）
var AUTO_COLUMN_NOTE =
  "※システムが自動算出・自動投入する項目です。手動編集はシステム算出値と衝突する恐れがあるため非推奨です。";

function setupTab_(ss, tab) {
  var sheet = ss.getSheetByName(tab.sheetName);
  if (!sheet) {
    sheet = ss.insertSheet(tab.sheetName);
  }
  sheet.getRange(1, 1, 1, tab.headers.length).setValues([tab.headers]);
  sheet.setFrozenRows(1);
  markAutoColumns_(sheet, tab);
  // 再実行してもべき等になるよう、ヘッダー行の値のみ上書きする（列削除等の破壊的操作は
  // 既存データを損なうリスクがあるため行わない）。
}

/**
 * AUTO項目（src/db_schema側でシステムが自動算出・自動投入すると定義されている項目。
 * tabDefinitions.js の autoColumns を参照）の列に、警告色の背景とセルコメントを付け、
 * 営業担当が誤って手入力しないよう視覚的に注意喚起する。
 * Range.protect() による書き込み保護までは今回のスコープ外（視覚的な警告のみ）。
 */
function markAutoColumns_(sheet, tab) {
  var autoColumns = tab.autoColumns || [];
  if (autoColumns.length === 0) return;

  var maxRows = sheet.getMaxRows();
  autoColumns.forEach(function (columnName) {
    var columnIndex = tab.headers.indexOf(columnName) + 1; // 1-indexed
    if (columnIndex <= 0) return;

    sheet.getRange(1, columnIndex).setNote(AUTO_COLUMN_NOTE);
    var numRows = maxRows; // ヘッダー行込みで列全体に警告色を付ける
    sheet.getRange(1, columnIndex, numRows, 1).setBackground(AUTO_COLUMN_BACKGROUND);
  });
}

function installEditTrigger_(ss) {
  // setupAllの再実行で同一トリガーが重複登録されないよう、既存の同名トリガーを一旦削除する。
  ScriptApp.getProjectTriggers()
    .filter(function (trigger) {
      return trigger.getHandlerFunction() === "onEditSync";
    })
    .forEach(function (trigger) {
      ScriptApp.deleteTrigger(trigger);
    });

  ScriptApp.newTrigger("onEditSync").forSpreadsheet(ss).onEdit().create();
}
