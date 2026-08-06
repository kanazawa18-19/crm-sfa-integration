"use strict";

/**
 * payloadUtils.js / tabDefinitions.js の純粋関数の単体テスト。
 * GASにはpytest/Jest相当の実行環境が無いため、Node.js組み込みのテストランナーを使う。
 *
 * 実行方法: node --test gas/payloadUtils.test.js
 */

const test = require("node:test");
const assert = require("node:assert/strict");

const {
  isHeaderRowEdit,
  isSyncManagedSheet,
  rowValuesToRecord,
  dataRowNumbersInRange,
  buildEditPayload,
  isWithinSyncWriteWindow,
} = require("./payloadUtils.js");

const { BUSINESS_SHEET_NAMES, ALL_TABS, DELETE_FLAG_COLUMN, SYNC_LOG_TAB } = require("./tabDefinitions.js");

test("isHeaderRowEdit: 1行目はヘッダー行として編集検知の対象外", () => {
  assert.equal(isHeaderRowEdit(1), true);
  assert.equal(isHeaderRowEdit(2), false);
  assert.equal(isHeaderRowEdit(42), false);
});

test("isSyncManagedSheet: 業務タブのみtrue、分析・クロスセル・同期ログはfalse", () => {
  assert.equal(isSyncManagedSheet("案件管理", BUSINESS_SHEET_NAMES), true);
  assert.equal(isSyncManagedSheet("分析", BUSINESS_SHEET_NAMES), false);
  assert.equal(isSyncManagedSheet("クロスセル対象リスト", BUSINESS_SHEET_NAMES), false);
  assert.equal(isSyncManagedSheet("同期ログ", BUSINESS_SHEET_NAMES), false);
});

test("rowValuesToRecord: ヘッダー名をキーにした値マッピングを組み立てる", () => {
  const headers = ["案件ID", "営業ステータス", "初期費用（イニシャル）"];
  const rowValues = ["MSA-PJ-001", "提案中", 500000];

  assert.deepEqual(rowValuesToRecord(headers, rowValues), {
    "案件ID": "MSA-PJ-001",
    "営業ステータス": "提案中",
    "初期費用（イニシャル）": 500000,
  });
});

test("rowValuesToRecord: 空ヘッダー列は無視する", () => {
  const headers = ["案件ID", "", "営業ステータス"];
  const rowValues = ["MSA-PJ-001", "予備列の値", "提案中"];

  assert.deepEqual(rowValuesToRecord(headers, rowValues), {
    "案件ID": "MSA-PJ-001",
    "営業ステータス": "提案中",
  });
});

test("rowValuesToRecord: rowValuesがheadersより短い場合は空文字で埋める", () => {
  const headers = ["案件ID", "営業ステータス"];
  const rowValues = ["MSA-PJ-001"];

  assert.deepEqual(rowValuesToRecord(headers, rowValues), {
    "案件ID": "MSA-PJ-001",
    "営業ステータス": "",
  });
});

test("rowValuesToRecord: excludedHeadersで指定した列（削除フラグ等）はpayloadのvaluesから除外する", () => {
  const headers = ["案件ID", "営業ステータス", DELETE_FLAG_COLUMN];
  const rowValues = ["MSA-PJ-001", "提案中", false];

  const record = rowValuesToRecord(headers, rowValues, {
    excludedHeaders: [DELETE_FLAG_COLUMN],
  });

  assert.deepEqual(record, {
    "案件ID": "MSA-PJ-001",
    "営業ステータス": "提案中",
  });
  assert.equal(Object.prototype.hasOwnProperty.call(record, DELETE_FLAG_COLUMN), false);
});

test("rowValuesToRecord: optionsを省略した場合は削除フラグ列も通常通り含める（後方互換）", () => {
  const headers = ["案件ID", DELETE_FLAG_COLUMN];
  const rowValues = ["MSA-PJ-001", false];

  assert.deepEqual(rowValuesToRecord(headers, rowValues), {
    "案件ID": "MSA-PJ-001",
    [DELETE_FLAG_COLUMN]: false,
  });
});

test("rowValuesToRecord: formatDate指定時はDate値を文字列化する（UTC変換によるオフバイワン日ズレ対策）", () => {
  const headers = ["契約日", "案件名"];
  const rowValues = [new Date(2026, 7, 5), "テスト案件"]; // ローカルタイムゾーンのDateオブジェクト
  const formatDate = (date) => `${date.getFullYear()}-${date.getMonth() + 1}-${date.getDate()}`;

  assert.deepEqual(rowValuesToRecord(headers, rowValues, { formatDate }), {
    "契約日": "2026-8-5",
    "案件名": "テスト案件",
  });
});

test("rowValuesToRecord: formatDate未指定時はDate値をそのまま格納する", () => {
  const headers = ["契約日"];
  const date = new Date(2026, 7, 5);
  const rowValues = [date];

  assert.deepEqual(rowValuesToRecord(headers, rowValues), { "契約日": date });
});

test("dataRowNumbersInRange: 単一行編集はその1行のみを返す", () => {
  assert.deepEqual(dataRowNumbersInRange(5, 1), [5]);
});

test("dataRowNumbersInRange: 複数行の貼り付け・オートフィルは範囲内の全行を返す", () => {
  assert.deepEqual(dataRowNumbersInRange(3, 4), [3, 4, 5, 6]);
});

test("dataRowNumbersInRange: ヘッダー行を含む範囲はヘッダー行だけを除外しデータ行は処理する", () => {
  assert.deepEqual(dataRowNumbersInRange(1, 3), [2, 3]);
});

test("dataRowNumbersInRange: ヘッダー行のみの編集は空配列を返す", () => {
  assert.deepEqual(dataRowNumbersInRange(1, 1), []);
});

test("buildEditPayload: spreadsheet_payload_to_sync_event が期待する形式を組み立てる", () => {
  const payload = buildEditPayload("案件管理", 42, "2026-08-05T09:00:00+09:00", {
    "案件ID": "MSA-PJ-001",
  });

  assert.deepEqual(payload, {
    sheet: "案件管理",
    row: 42,
    editedAt: "2026-08-05T09:00:00+09:00",
    values: { "案件ID": "MSA-PJ-001" },
  });
});

test("isWithinSyncWriteWindow: マーク無しは常にfalse", () => {
  assert.equal(isWithinSyncWriteWindow(1000, null, 5000), false);
  assert.equal(isWithinSyncWriteWindow(1000, undefined, 5000), false);
});

test("isWithinSyncWriteWindow: TTL以内はtrue、TTL経過後はfalse", () => {
  assert.equal(isWithinSyncWriteWindow(4000, 1000, 5000), true); // 経過3000ms < TTL5000ms
  assert.equal(isWithinSyncWriteWindow(7000, 1000, 5000), false); // 経過6000ms >= TTL5000ms
});

test("tabDefinitions: 全タブのヘッダーに重複列が無い", () => {
  ALL_TABS.forEach((tab) => {
    const uniqueHeaders = new Set(tab.headers);
    assert.equal(
      uniqueHeaders.size,
      tab.headers.length,
      `${tab.sheetName} タブのヘッダーに重複がある: ${tab.headers.join(", ")}`
    );
  });
});

test("tabDefinitions: 業務タブは末尾に削除フラグ列を持つ", () => {
  const { BUSINESS_TABS } = require("./tabDefinitions.js");
  BUSINESS_TABS.forEach((tab) => {
    assert.equal(tab.headers[tab.headers.length - 1], DELETE_FLAG_COLUMN);
  });
});

test("tabDefinitions: 業務タブのautoColumnsは全てheadersに存在する列名である", () => {
  const { BUSINESS_TABS } = require("./tabDefinitions.js");
  BUSINESS_TABS.forEach((tab) => {
    (tab.autoColumns || []).forEach((columnName) => {
      assert.ok(
        tab.headers.indexOf(columnName) !== -1,
        `${tab.sheetName} タブのautoColumns "${columnName}" がheadersに存在しない`
      );
    });
  });
});

test("tabDefinitions: 同期ログタブの列はspreadsheet_sync.pyのappend_conflict_logと一致する", () => {
  assert.deepEqual(SYNC_LOG_TAB.headers, [
    "対象ID",
    "項目名",
    "採用値",
    "却下値",
    "却下元ツール",
    "発生日時",
  ]);
});
