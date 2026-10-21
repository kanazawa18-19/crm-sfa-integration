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

test('新しい毎時処理の結果は監視で再照会しない', () => {
  const f = fixture();
  f.context.ScriptApp.getProjectTriggers=()=>[{getHandlerFunction:()=> 'affiliationJobTick'}];
  f.context.affiliationJobTick();
  const count=f.calls.length;
  f.context.affiliationJobWatchdog();
  assert.equal(f.calls.length,count);
});

test('所属監視は古い結果や不正時刻・遅延・配送待ちを再照会する', () => {
  const now=Date.now();
  for (const extra of [
    {checkedAt:now-90*60000-1}, {checkedAt:now+1}, {durationMs:240001},
    {status:{enabled:true,counts:{writing:1},last_success_at:new Date(now).toISOString()}},
    {status:{enabled:true,counts:{},last_success_at:'invalid'}}
  ]) {
    const f=fixture();
    f.context.ScriptApp.getProjectTriggers=()=>[{getHandlerFunction:()=> 'affiliationJobTick'}];
    f.context.Date={now:()=>now,parse:Date.parse};
    f.props.set('AFFILIATION_LAST_TICK',String(now));
    f.props.set('AFFILIATION_MONITOR_SNAPSHOT',JSON.stringify({checkedAt:now,durationMs:1,resultStatus:'ok',status:{enabled:true,counts:{},last_success_at:new Date(now).toISOString()},...extra}));
    f.context.affiliationJobWatchdog();assert.equal(f.calls.length,1);
  }
});

test('所属実行が失敗したら前回の成功記録を使わない', () => {
  const f=fixture();f.props.set('AFFILIATION_MONITOR_SNAPSHOT','previous');
  f.context.UrlFetchApp.fetch=()=>{throw new Error('通信断');};
  assert.throws(()=>f.context.affiliationJobTick());
  assert.equal(f.props.get('AFFILIATION_MONITOR_SNAPSHOT'),'');
  assert.equal(f.sent.length,1);
});

test('所属監視は90分と4分の境界を含めて再利用し照会で鮮度を延ばさない', () => {
  const now=Date.now(),f=fixture();
  f.context.ScriptApp.getProjectTriggers=()=>[{getHandlerFunction:()=> 'affiliationJobTick'}];
  f.context.Date={now:()=>now,parse:Date.parse};
  f.props.set('AFFILIATION_LAST_TICK',String(now));
  const snapshot=JSON.stringify({checkedAt:now-90*60000,durationMs:240000,resultStatus:'ok',status:{enabled:true,counts:{},last_success_at:new Date(now-90*60000).toISOString()}});
  f.props.set('AFFILIATION_MONITOR_SNAPSHOT',snapshot);
  f.context.affiliationJobWatchdog();assert.equal(f.calls.length,0);
  f.context.Date={now:()=>now+1,parse:Date.parse};
  f.context.affiliationJobWatchdog();assert.equal(f.calls.length,1);
  assert.equal(f.props.get('AFFILIATION_MONITOR_SNAPSHOT'),snapshot);
});


test('所属の保存結果不明・解除保留を即時通知と監視に含める', () => {
  const f=fixture({counts:{released:1}});f.context.affiliationJobTick();
  assert.equal(f.sent.length,2);
  assert.match(f.sent.find(x=>x[1].includes('定期処理の結果'))[2],/保存結果不明・所属解除の確認待ち：1件/);
  assert.ok(f.context.affiliationJobUnhealthy_({enabled:true,counts:{released:1},last_success_at:new Date().toISOString()},Date.now()));
});

test('所属監視は開始直後の記録作成中を待ち、4分を超えた欠測を通知する', () => {
  const now=Date.now();
  for(const elapsed of [1,240001]){
    const f=fixture({last_success_at:new Date(now).toISOString()}); f.context.Date={now:()=>now,parse:Date.parse};
    f.context.ScriptApp.getProjectTriggers=()=>[{getHandlerFunction:()=> 'affiliationJobTick'}];
    f.props.set('AFFILIATION_LAST_TICK',String(now));
    f.props.set('AFFILIATION_RUN_STARTED',String(now-elapsed));
    f.context.affiliationJobWatchdog();
    assert.equal(f.sent.length,elapsed>240000?1:0);
  }
});

test('所属監視記録は巨大なエラーや巡回データを保存しない', () => {
  const f=fixture({last_error:'x'.repeat(20000),discovery:{data:'x'.repeat(20000)}});
  f.context.affiliationJobTick();
  assert.ok(f.props.get('AFFILIATION_MONITOR_SNAPSHOT').length<1000);
  assert.ok(JSON.parse(f.props.get('AFFILIATION_MONITOR_SNAPSHOT')).status.last_error);
  assert.equal(f.sent.length,2);
});
