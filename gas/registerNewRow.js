"use strict";

function onOpen() {
  SpreadsheetApp.getUi().createMenu("CRM同期")
    .addItem("選択した新規行を登録", "registerNewRow")
    .addToUi();
}

function registerNewRow() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sheet = ss.getActiveSheet();
  var row = sheet.getActiveRange().getRow();
  var ui = SpreadsheetApp.getUi();
  if (!isSyncManagedSheet(sheet.getName(), BUSINESS_SHEET_NAMES) || row < 2) {
    ui.alert("業務タブで新しく入力したデータ行を選んでください。");
    return;
  }
  // 空の同期キーだけで自動作成しない。本人の登録意図をメニューで確認する。
  if (ui.alert("新規行の登録", "この行を新規として登録します。既存レコードの同期キーを消した行には使わないでください。必須項目や重複を確認できない場合は保留します。", ui.ButtonSet.OK_CANCEL) !== ui.Button.OK) return;
  var lock = LockService.getDocumentLock();
  lock.waitLock(10000);
  try {
    var headers = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getValues()[0];
    var keyColumn = headers.indexOf("同期キー") + 1;
    if (!keyColumn) throw new Error("同期キー列がありません。");
    var keyCell = sheet.getRange(row, keyColumn);
    var key = String(keyCell.getValue() || "");
    if (key && key.indexOf("new:") !== 0) throw new Error("登録済みの同期キーがある行です。");
    if (!key) {
      key = "new:" + Utilities.getUuid();
      // 行に付けた識別情報は並べ替え・行移動に追随し、サーバーは行番号で書き込まない。
      sheet.getRange(row, 1, 1, sheet.getMaxColumns())
        .addDeveloperMetadata("CRM_NEW_REGISTRATION", key, SpreadsheetApp.DeveloperMetadataVisibility.DOCUMENT);
      keyCell.setValue(key);
    }
    var statusColumn = headers.indexOf("登録状況") + 1;
    if (!statusColumn) {
      statusColumn = sheet.getLastColumn() + 1;
      if (statusColumn > sheet.getMaxColumns()) sheet.insertColumnAfter(sheet.getMaxColumns());
      sheet.getRange(1, statusColumn).setValue("登録状況");
    }
    sheet.getRange(row, statusColumn).setValue("登録の受付を確認中です。結果はこの列へ表示します。");
    SpreadsheetApp.flush();
  } finally {
    lock.releaseLock();
  }
    try {
      postToWebhook_({sheet: sheet.getName(), row: row, editedAt: new Date().toISOString(),
        action: "register_new", values: {"同期キー": key}});
    } catch (error) {
      var message = "受付結果を確認できません。同じ行の登録状況を確認し、登録待ちなら再登録してください。";
      // 通信中の行移動やサーバー側の完了を考慮し、元の行番号には書き込まない。
      try {
        var matches = sheet.createDeveloperMetadataFinder()
          .withKey("CRM_NEW_REGISTRATION").withValue(key).find();
        if (matches.length === 1) {
          var currentRow = matches[0].getLocation().getRow();
          if (currentRow && String(currentRow.getCell(1, keyColumn).getValue()) === key) {
            var statusCell = currentRow.getCell(1, statusColumn);
            if (statusCell.getValue() === "登録の受付を確認中です。結果はこの列へ表示します。") {
              statusCell.setValue(message);
            }
          }
        }
      } catch (statusError) { /* 表示更新の失敗も、認証情報を含む例外本文は表示しない。 */ }
      ui.alert(message);
      throw new Error(message);
    }
}
