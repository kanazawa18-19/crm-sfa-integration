const {test} = require('node:test');
const assert = require('node:assert/strict');
const health = require('./domain');
const H = 3600000, now = Date.parse('2026-09-27T09:00:00Z');
const pending = {key:'notion_delivery', status:'pending', reason:'待機', last_received_at:'2026-09-26T00:00:00Z', activity_at:'2026-09-27T08:00:00Z'};
test('初回は保留、継続編集で猶予が延びず2時間後に警告', () => {
 const p = health.plan([pending], null, now); assert.equal(p.events.length, 0);
 const p2 = health.plan([{...pending, activity_at:new Date(now+H).toISOString()}], p.next, now+H);
 assert.equal(p2.events.length, 1);
 assert.equal(health.plan([pending], p2.next, now+2*H).events.length, 0);
 assert.equal(health.plan([pending], p2.next, now+25*H).events.length, 1);
});
test('購読切れは即時、取得不能は次回まで待つ', () => {
 assert.equal(health.plan([{key:'zoho_subscription', status:'critical', reason:'失効'}], null, now).events.length, 1);
 const c = {key:'monitor',status:'unknown',reason:'接続失敗'};
 const p=health.plan([c],null,now);assert.equal(p.events.length,0);
 assert.equal(health.plan([c],p.next,now+H).events.length,1);
});
test('確認不能を復旧と言わず、正常復帰は1回', () => {
 const p=health.plan([{key:'notion_delivery',status:'critical',reason:'異常'}],null,now);
 assert.equal(health.plan([{key:'notion_delivery',status:'unverified',reason:'不明'}],p.next,now+H).events.length,0);
 const good={key:'notion_delivery',status:'ok',reason:'受信'};
 const p2=health.plan([good],p.next,now+H);assert.equal(p2.events[0].level,'recovered');
 assert.equal(health.plan([good],p2.next,now+2*H).events.length,0);
});
test('JSTで日次報告、Slack失敗で前状態を残せば再送できる', () => {
 const p=health.plan([],null,now);assert.equal(p.daily,true);
 assert.equal(health.plan([],p.next,now+H).daily,false);
 assert.equal(health.plan([],p.next,now+24*H).daily,true);
 assert.equal(health.plan([],null,now+H).daily,true);
});
test('APIの項目欠落・古い結果を正常扱いしない', () => {
 assert.throws(()=>health.validate({schema_version:1,observed_at:new Date(now).toISOString(),checks:[]},now));
});

test('警告後に受信が進めば復旧し、別の停滞は24時間待たず通知', () => {
 const p=health.plan([pending],null,now);
 const warned=health.plan([pending],p.next,now+2*H);
 const resumed={...pending,last_received_at:new Date(now+2*H).toISOString(),activity_at:new Date(now+3*H).toISOString()};
 const recovered=health.plan([resumed],warned.next,now+3*H);
 assert.equal(recovered.events[0].level,'recovered');
 const stopped=health.plan([resumed],recovered.next,now+5*H);
 assert.equal(stopped.events[0].level,'warning');
});
test('API全体が停止しても個別の未解消警告を保持', () => {
 const prev=health.plan([{key:'zoho_subscription',status:'critical',reason:'失効'}],null,now);
 const failed=health.plan([{key:'monitor',status:'unknown',reason:'不能'}],prev.next,now+H);
 assert.deepEqual(failed.next.checks.zoho_subscription,prev.next.checks.zoho_subscription);
});
