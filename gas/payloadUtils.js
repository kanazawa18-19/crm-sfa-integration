"use strict";

/**
 * GAS固有API（SpreadsheetApp/UrlFetchApp/PropertiesService等）に依存しない純粋関数群。
 * onEdit.js からはGASの薄いラッパーとして呼び出す。
 *
 * GASにはJest/pytest相当の実行環境が無いため、ロジック部分をこのファイルへ切り出し、
 * Node.jsの組み込みテストランナーで検証する（`node --test gas/`）。
 */

var DATA_START_ROW = 2; // 1行目はヘッダー行のため、編集検知の対象外とする

function isHeaderRowEdit(row) {
  return row < DATA_START_ROW;
}

function isSyncManagedSheet(sheetName, businessSheetNames) {
  return businessSheetNames.indexOf(sheetName) !== -1;
}

/**
 * ヘッダー行と行の値配列から、HttpSpreadsheetClient（Python側）と同じ
 * 「ヘッダー名をキーにしたレコード」表現を組み立てる。
 * 空ヘッダー列（"" や未定義）は無視する（列の後方に予備列がある運用を想定）。
 *
 * options:
 *   - excludedHeaders: この名前のヘッダー列はレコードに含めない（例: 削除フラグ列。
 *     削除フラグは delete_record 専用の仕組みであり、通常の編集フローで同期対象
 *     プロパティとしてWebhookへ送るとPython側のDBスキーマに存在せずKeyErrorになる）。
 *   - formatDate: Date値をこの関数で文字列化してから格納する（GAS依存のため呼び出し側
 *     [onEdit.js] から注入する。未指定ならDate値をそのまま格納する）。JSON.stringifyに
 *     Dateオブジェクトを渡すとUTCへ変換されタイムゾーンによっては日付が1日ズレるため、
 *     スプレッドシートのタイムゾーンを基準にした明示的な日付文字列へ変換する用途。
 */
function rowValuesToRecord(headers, rowValues, options) {
  options = options || {};
  var excludedHeaders = options.excludedHeaders || [];
  var formatDate = options.formatDate;

  var record = {};
  headers.forEach(function (name, index) {
    if (!name) return;
    if (excludedHeaders.indexOf(name) !== -1) return;

    var value = index < rowValues.length ? rowValues[index] : "";
    if (formatDate && value instanceof Date) {
      value = formatDate(value);
    }
    record[name] = value;
  });
  return record;
}

/**
 * 編集範囲（開始行・行数）に含まれる「データ行」（ヘッダー行を除く）の行番号一覧を返す。
 *
 * GASのonEditは複数行にまたがる貼り付け・オートフィルでも1回しか発火せず、e.rangeが
 * 編集範囲全体を表す。isHeaderRowEdit(range.getRow()) のように開始行だけを見ると、
 * 2行目以降が処理されず静かに同期漏れする（貼り付け時）、またはヘッダー行を含む範囲を
 * 編集すると範囲全体がスキップされる（ヘッダー行込みの貼り付け時）という2つの不具合が
 * 生じるため、この関数で範囲内の各行を個別に判定する。
 */
function dataRowNumbersInRange(startRow, numRows) {
  var rows = [];
  for (var i = 0; i < numRows; i++) {
    var row = startRow + i;
    if (!isHeaderRowEdit(row)) rows.push(row);
  }
  return rows;
}

/**
 * spreadsheet_payload_to_sync_event（src/sync_engine/webhook_handlers/spreadsheet_webhook.py）
 * が期待するペイロード形式（sheet/row/editedAt/values）を組み立てる。
 */
function buildEditPayload(sheetName, row, editedAtIso, values) {
  return {
    sheet: sheetName,
    row: row,
    editedAt: editedAtIso,
    values: values,
  };
}

/**
 * 無限ループ防止の二次防御（PropertiesServiceフラグ）判定の純粋ロジック部分。
 *
 * 主たる防御はGASプラットフォーム自体の仕様（Sheets API経由の書き込みやスクリプトによる
 * Range.setValue()等は、simple/installable問わずonEditトリガーを発火させない）に依っており、
 * spreadsheet_webhook.py 側のコメントの通り、この経路では無限ループはほぼ発生しない。
 * この関数はあくまで将来GAS側から書き込みを行う経路（例: 同期エンジンがSheets APIではなく
 * GAS Web AppのdoPost経由でSpreadsheetAppを呼ぶ構成に変わった場合）に備えた保険的な
 * 二次防御であり、その判定ロジック（マーク時刻からTTL以内かどうか）のみを担う。
 *
 * markedAtMsが無い（=書き込みマークが無い）場合や、TTLを過ぎている場合はfalseを返す。
 */
function isWithinSyncWriteWindow(nowMs, markedAtMs, ttlMs) {
  if (markedAtMs === null || markedAtMs === undefined) return false;
  return nowMs - markedAtMs < ttlMs;
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    DATA_START_ROW: DATA_START_ROW,
    isHeaderRowEdit: isHeaderRowEdit,
    isSyncManagedSheet: isSyncManagedSheet,
    rowValuesToRecord: rowValuesToRecord,
    dataRowNumbersInRange: dataRowNumbersInRange,
    buildEditPayload: buildEditPayload,
    isWithinSyncWriteWindow: isWithinSyncWriteWindow,
  };
}
