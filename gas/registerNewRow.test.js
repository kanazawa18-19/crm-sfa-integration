"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const vm = require("node:vm");
const fs = require("node:fs");

function environment(key = "") {
  const calls = [];
  const headers = ["グループ名", "同期キー", "登録状況"];
  const ui = {
    Button: {OK: "OK"}, ButtonSet: {OK_CANCEL: "OK_CANCEL"},
    alert: () => "OK",
    createMenu: name => ({addItem(label, handler) {calls.push(["menu", name, label, handler]); return this;}, addToUi() {}}),
  };
  const sheet = {
    getName: () => "チェーン", getActiveRange: () => ({getRow: () => 7}),
    getLastColumn: () => 3, getMaxColumns: () => 3,
    getRange(row, col, height, width) {
      return {getValues: () => [headers], getValue: () => key,
        setValue(value) {calls.push(["set", row, col, value]);},
        addDeveloperMetadata(name, value, visibility) {calls.push(["metadata", name, value, visibility]);},
      };
    },
  };
  const context = vm.createContext({
    SpreadsheetApp: {getActiveSpreadsheet: () => ({getActiveSheet: () => sheet}), getUi: () => ui,
      DeveloperMetadataVisibility: {DOCUMENT: "DOCUMENT"}, flush() {}},
    LockService: {getDocumentLock: () => ({waitLock() {}, releaseLock() {calls.push(["unlock"]);}})},
    BUSINESS_SHEET_NAMES: ["チェーン"], isSyncManagedSheet: () => true,
    Utilities: {getUuid: () => "sample-uuid"},
    postToWebhook_: payload => calls.push(["post", payload]),
  });
  vm.runInContext(fs.readFileSync(__dirname + "/registerNewRow.js", "utf8"), context);
  return {context, calls};
}

test("メニューから明示的に登録する入口を出す", () => {
  const {context, calls} = environment(); context.onOpen();
  assert.equal(calls[0][3], "registerNewRow");
});

test("行の識別情報を作ってから新規キーを書き込み、登録要求を送る", () => {
  const {context, calls} = environment(); context.registerNewRow();
  const metadata = calls.findIndex(c => c[0] === "metadata");
  const key = calls.findIndex(c => c[0] === "set" && c[2] === 2);
  assert.ok(metadata >= 0 && metadata < key);
  const payload = calls.find(c => c[0] === "post")[1];
  assert.equal(payload.action, "register_new");
  assert.equal(payload.values["同期キー"], "new:sample-uuid");
});

test("既存の同期キーを持つ行は新規として作らない", () => {
  const {context, calls} = environment("existing-notion-id");
  assert.throws(() => context.registerNewRow(), /登録済み/);
  assert.equal(calls.some(c => c[0] === "metadata" || c[0] === "post"), false);
  assert.equal(calls.at(-1)[0], "unlock");
});

test("登録待ちの再送ではキーや行の識別情報を作り直さない", () => {
  const {context, calls} = environment("new:existing-request"); context.registerNewRow();
  assert.equal(calls.some(c => c[0] === "metadata"), false);
  assert.equal(calls.some(c => c[0] === "set" && c[2] === 2), false);
  assert.equal(calls.find(c => c[0] === "post")[1].values["同期キー"], "new:existing-request");
});

for (const scenario of ["移動", "完了", "重複"]) {
  test("受付結果不明時に固定文を表示する: " + scenario, () => {
    const {context, calls} = environment("new:existing-request");
    const sheet = context.SpreadsheetApp.getActiveSpreadsheet().getActiveSheet();
    const pending = "登録の受付を確認中です。結果はこの列へ表示します。";
    const current = {getCell: (_row, col) => ({
      getValue: () => col === 2 ? (scenario === "完了" ? "notion-id" : "new:existing-request") : pending,
      setValue: value => calls.push(["current-status", value]),
    })};
    const metadata = {getLocation: () => ({getRow: () => current})};
    sheet.createDeveloperMetadataFinder = () => ({
      withKey() {return this;}, withValue() {return this;},
      find: () => scenario === "重複" ? [metadata, metadata] : [metadata],
    });
    context.postToWebhook_ = () => {throw new Error("secret response");};
    assert.throws(() => context.registerNewRow(), /受付結果を確認できません/);
    assert.equal(calls.filter(c => c[0] === "current-status").length, scenario === "移動" ? 1 : 0);
    assert.equal(JSON.stringify(calls).includes("secret response"), false);
    assert.equal(calls.some(c => c[0] === "unlock"), true);
  });
}


test("通信の前に文書ロックを解放する", () => {
  const {context,calls}=environment();
  context.registerNewRow();
  assert.ok(calls.findIndex(c=>c[0]==="unlock") < calls.findIndex(c=>c[0]==="post"));
});
