/* 専用トリガー、二重実行、失敗監視をローカルで確認する。 */
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const test = require('node:test');

function fixture(statusOverrides = {}) {
  const props = new Map();
  const calls = [];
  const sent = [];
  let locked = false;
  const context = {
    console: {log() {}}, Date, JSON,
    ScriptApp: {getIdentityToken: () => 'synthetic', getProjectTriggers: () => []},
    PropertiesService: {getScriptProperties: () => ({getProperty: k => props.get(k), setProperty: (k,v) => props.set(k,v)})},
    LockService: {getScriptLock: () => ({tryLock: () => !locked, releaseLock() {}})},
    Session: {getEffectiveUser: () => ({getEmail: () => 'self@example.invalid'})},
    MailApp: {sendEmail: (...args) => sent.push(args)},
    UrlFetchApp: {fetch(url) { calls.push(url); return {getResponseCode: () => 200, getContentText: () => JSON.stringify(
      url.endsWith('/run') ? {status: 'ok', counts: {held: 1}, discovered: 100, duration_ms: 1} :
        {enabled: true, dry_run: true, counts: {held: 4}, retry_count: 0, last_success_at: new Date().toISOString(), ...statusOverrides})};}}
  };
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(__dirname + '/Code.js', 'utf8'), context);
  return {context, calls, props, sent, lock: () => { locked = true; }};
}

test('毎時最大4バッチと日次の結果通知', () => {
  const f = fixture();
  f.context.affiliationJobTick();
  assert.equal(f.calls.filter(x => x.endsWith('/run')).length, 4);
  assert.equal(f.sent.length, 1);
  assert.match(f.sent[0][1], /定期処理の結果/);
  f.context.affiliationJobTick();
  assert.equal(f.sent.length, 1);
});

test('同時実行は通信も通知もしない', () => {
  const f = fixture(); f.lock(); f.context.affiliationJobTick();
  assert.equal(f.calls.length, 0); assert.equal(f.sent.length, 0);
});

test('トリガー消失を本人へ通知', () => {
  const f = fixture(); f.context.affiliationJobWatchdog();
  assert.equal(f.sent.length, 1);
  assert.match(f.sent[0][1], /要確認/);
});


test('配送待ちの日も未完件数を含む結果通知を続ける', () => {
  const f = fixture({counts: {delivery_pending: 1, verified: 2}});
  f.context.affiliationJobTick();
  assert.equal(f.sent.length, 2);
  const summary = f.sent.find(row => row[1].includes('定期処理の結果'));
  assert.match(summary[2], /配送確認待ち：1件/);
  assert.match(summary[2], /保存・配送確認済み：2件/);
});
