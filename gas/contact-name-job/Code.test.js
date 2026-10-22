/* GASを模した環境で、再試行・完了・認証漏れ・監視を確認する。 */
const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
function harness(states, http=200) {
  const props={};const sent=[];const calls=[];const deleted=[];
  const trigger={getHandlerFunction:()=> 'nameJobTick'};
  const c={console:{log:()=>{}},Date,JSON,Number,Error,
    ScriptApp:{getIdentityToken:()=> 'signed',getProjectTriggers:()=>[trigger],deleteTrigger:t=>deleted.push(t)},
    PropertiesService:{getScriptProperties:()=>({getProperty:k=>props[k],setProperty:(k,v)=>{props[k]=v;}})},
    Session:{getEffectiveUser:()=>({getEmail:()=> 'owner@example.invalid'})},
    GmailApp:{sendEmail:(...a)=>sent.push(a)},
    LockService:{getScriptLock:()=>({tryLock:()=>true,releaseLock:()=>{}})},
    UrlFetchApp:{fetch:(url,req)=>{calls.push(req);return response(states.shift(),http);},fetchAll:reqs=>{calls.push(...reqs);return reqs.map(()=>response({status:'ok'},http));}}};
  vm.createContext(c);vm.runInContext(fs.readFileSync(__dirname+'/Code.js','utf8'),c);
  return {c,props,sent,calls,deleted};
}
function response(data,code){return {getResponseCode:()=>code,getContentText:()=>JSON.stringify(data)};}
const running={status:'running',counts:{retry:0,failed:0},remaining:10};
test('4系統を認証付きで起動し、進捗を保存',()=>{
  const h=harness([running,running]);h.c.nameJobTick();
  assert.equal(h.calls.filter(x=>x.method==='post').length,4);
  assert.ok(h.calls.every(x=>x.headers.Authorization==='Bearer signed' && x.followRedirects===false));
  assert.ok(h.props.NAME_JOB_LAST_TICK);assert.equal(h.sent.length,0);
});
test('全件完了の通知後に定期実行を解除',()=>{
  const h=harness([running,{status:'completed',counts:{verified:10}}]);h.c.nameJobTick();
  assert.equal(h.sent.length,1);assert.equal(h.deleted.length,1);
});
test('接続失敗は通知し、トリガーを残して次回再試行',()=>{
  const h=harness([running],503);assert.throws(()=>h.c.nameJobTick());
  assert.equal(h.sent.length,1);assert.equal(h.deleted.length,0);assert.equal(h.props.NAME_JOB_LAST_TICK,undefined);
});
test('保留中は実行しない',()=>{
  const paused={status:'paused',counts:{retry:0,failed:0}};
  const h=harness([paused,paused]);h.c.nameJobTick();assert.equal(h.calls.filter(x=>x.method==='post').length,0);
});
test('通知失敗なら完了扱いでトリガーを削除しない',()=>{
  const done={status:'completed',counts:{verified:10}};const h=harness([done,done]);
  h.c.GmailApp.sendEmail=()=>{throw new Error('メール利用枠不足');};
  assert.throws(()=>h.c.nameJobTick());assert.equal(h.deleted.length,0);
});
test('一系統の失敗でも処理後の件数を取得して通知する',()=>{
  const after={...running,counts:{verified:7,retry:1,failed:0}};
  const h=harness([running,after]);
  h.c.UrlFetchApp.fetchAll=()=>[response({},503),response({},200),response({},200),response({},200)];
  h.c.nameJobTick();
  assert.equal(JSON.parse(h.props.NAME_JOB_LAST_STATUS).counts.verified,7);
  assert.match(h.sent[0][2],/保存確認済み：7件/);
  assert.equal(h.deleted.length,0);
});
test('少数の一時的な再試行では通知を増やさない',()=>{
  const h=harness([running,{...running,counts:{retry:1,failed:0}}]);h.c.nameJobTick();
  assert.equal(h.sent.length,0);
});
test('停止通知には自動再開するとの誤案内を含めない',()=>{
  const paused={status:'paused',counts:{retry:0,failed:0}};
  const h=harness([paused,paused]);h.c.nameJobTick();
  assert.match(h.sent[0][2],/定期実行を停止しました/);
  assert.doesNotMatch(h.sent[0][2],/定期実行は継続/);
  assert.equal(h.deleted.length,1);
});

function healthyStatus() {
  return {...running,last_success_at:new Date().toISOString()};
}
function monitorHarness(snapshot) {
  const h=harness([healthyStatus()]);
  h.props.NAME_JOB_LAST_TICK=String(Date.now());
  h.props.NAME_JOB_MONITOR_SNAPSHOT=JSON.stringify(snapshot);
  return h;
}
test('実処理直後の監視は追加通信せず、完了確認済みの進捗を使う',()=>{
  const status=healthyStatus();
  const h=harness([status,status]);h.c.nameJobTick();
  const before=h.calls.length;
  h.c.nameJobWatchdog();h.c.nameJobWatchdog();
  assert.equal(h.calls.length,before);assert.equal(h.sent.length,0);
});
test('古い・欠損・壊れた監視記録は最新状態を再照会する',()=>{
  const now=Date.now();
  for(const snapshot of [
    null,{},
    {checkedAt:now-21*60000,durationMs:100,status:healthyStatus()},
    {checkedAt:now+60000,durationMs:100,status:healthyStatus()},
    {checkedAt:now,durationMs:-1,status:healthyStatus()},
    {checkedAt:now,durationMs:100,status:{...healthyStatus(),status:'completed'}}
  ]) {
    const h=monitorHarness(snapshot);h.c.nameJobWatchdog();
    assert.equal(h.calls.length,1);
    assert.equal(h.props.NAME_JOB_MONITOR_SNAPSHOT,JSON.stringify(snapshot));
  }
  const h=monitorHarness(null);h.props.NAME_JOB_MONITOR_SNAPSHOT='{broken';
  h.c.nameJobWatchdog();assert.equal(h.calls.length,1);
});
test('監視の再照会成功で古い実処理の鮮度を延長しない',()=>{
  const h=harness([healthyStatus(),healthyStatus()]);
  h.props.NAME_JOB_LAST_TICK=String(Date.now()-21*60000);
  h.c.nameJobWatchdog();h.c.nameJobWatchdog();
  assert.equal(h.calls.length,2);
  assert.equal(h.props.NAME_JOB_MONITOR_SNAPSHOT,undefined);
  assert.equal(h.sent.length,1);
});
test('トリガー欠落時は新しい結果があっても通知し再照会する',()=>{
  const h=monitorHarness({checkedAt:Date.now(),durationMs:100,status:healthyStatus()});
  h.c.ScriptApp.getProjectTriggers=()=>[];
  h.c.nameJobWatchdog();
  assert.equal(h.calls.length,1);assert.match(h.sent[0][1],/定期処理停止/);
});
test('成功時刻の欠損・不正・未来・遅延を正常と扱わない',()=>{
  for(const value of [null,'invalid',new Date(Date.now()+60000).toISOString(),new Date(Date.now()-31*60000).toISOString()]) {
    const bad={...running,last_success_at:value};
    const h=harness([bad]);
    h.props.NAME_JOB_LAST_TICK=String(Date.now());
    h.props.NAME_JOB_MONITOR_SNAPSHOT=JSON.stringify({checkedAt:Date.now(),durationMs:100,status:bad});
    h.c.nameJobWatchdog();
    assert.equal(h.calls.length,1);assert.match(h.sent[0][1],/進捗停止/);
  }
});
test('所要時間が4分を超えた実処理は通知し再照会する',()=>{
  const h=monitorHarness({checkedAt:Date.now(),durationMs:240001,status:healthyStatus()});
  h.c.nameJobWatchdog();
  assert.equal(h.calls.length,1);assert.match(h.sent[0][1],/処理遅延/);
});
test('最新実行の接続失敗後は以前の成功結果を再利用しない',()=>{
  const h=harness([running],503);
  h.props.NAME_JOB_MONITOR_SNAPSHOT=JSON.stringify({checkedAt:Date.now(),durationMs:100,status:healthyStatus()});
  assert.throws(()=>h.c.nameJobTick());
  assert.equal(h.props.NAME_JOB_MONITOR_SNAPSHOT,'');
  assert.throws(()=>h.c.nameJobWatchdog());
  assert.equal(h.calls.length,2);
});
test('一系統の失敗後は成功した状態取得だけで監視を省略しない',()=>{
  const status=healthyStatus();
  const h=harness([status,status,status]);
  h.c.UrlFetchApp.fetchAll=()=>[response({},503),response({},200),response({},200),response({},200)];
  h.c.nameJobTick();
  assert.equal(h.props.NAME_JOB_MONITOR_SNAPSHOT,'');
  h.c.nameJobWatchdog();assert.equal(h.calls.length,3);
});
test('監視接続の失敗は秘密や応答本文を含めず通知する',()=>{
  const h=harness([],503);h.props.NAME_JOB_LAST_TICK=String(Date.now());
  assert.throws(()=>h.c.nameJobWatchdog(),/監視に失敗/);
  assert.equal(h.sent.length,1);assert.match(h.sent[0][1],/接続エラー/);
  assert.doesNotMatch(h.sent[0][2],/signed/);
});
test('再照会中に並行実行が成功しても未来時刻として誤通知しない',()=>{
  const h=harness([]);
  let now=Date.now();
  h.c.Date={now:()=>now,parse:Date.parse};
  h.props.NAME_JOB_LAST_TICK=String(now);
  h.c.UrlFetchApp.fetch=()=>{
    now+=1000;
    return response({...running,last_success_at:new Date(now).toISOString()},200);
  };
  h.c.nameJobWatchdog();
  assert.equal(h.sent.length,0);
});
test('鮮度20分・成功30分・所要時間4分の境界を超えたときだけ再照会する',()=>{
  const now=Date.now();
  for(const [age,successAge,duration,expected] of [
    [20*60000,30*60000,240000,0],
    [20*60000+1,0,100,1],
    [0,30*60000+1,100,1],
    [0,0,240001,1]
  ]) {
    const h=monitorHarness({checkedAt:now-age,durationMs:duration,
      status:{...running,last_success_at:new Date(now-successAge).toISOString()}});
    h.c.Date={now:()=>now,parse:Date.parse};
    h.props.NAME_JOB_LAST_TICK=String(now);
    h.c.nameJobWatchdog();assert.equal(h.calls.length,expected);
  }
});
test('過去の遅延記録は現在の処理遅延として繰り返し通知しない',()=>{
  const h=monitorHarness({checkedAt:Date.now()-21*60000,durationMs:240001,status:healthyStatus()});
  h.c.nameJobWatchdog();
  assert.equal(h.calls.length,1);assert.equal(h.sent.length,0);
});
test('不正な最終実行時刻や重複トリガーでは成功記録を再利用しない',()=>{
  for(const tick of ['invalid','0',String(Date.now()+60000)]) {
    const h=monitorHarness({checkedAt:Date.now(),durationMs:100,status:healthyStatus()});
    h.props.NAME_JOB_LAST_TICK=tick;h.c.nameJobWatchdog();
    assert.equal(h.calls.length,1);assert.match(h.sent[0][1],/定期処理停止/);
  }
  const h=monitorHarness({checkedAt:Date.now(),durationMs:100,status:healthyStatus()});
  h.c.ScriptApp.getProjectTriggers=()=>[1,2].map(()=>({getHandlerFunction:()=> 'nameJobTick'}));
  h.c.nameJobWatchdog();
  assert.equal(h.calls.length,1);assert.match(h.sent[0][1],/定期処理停止/);
});

test('通知元だけを個人Gmailに変更し既存宛先と送信失敗の再試行を維持',()=>{
 const h=harness([]);h.c.nameJobNotify_('接続エラー',{});assert.equal(h.sent[0][0],'owner@example.invalid');assert.equal(h.sent[0][3].from,'daizuhakkou0619@gmail.com');
 const rejected=harness([]);let fallback=0;rejected.c.MailApp={sendEmail:()=>fallback++};rejected.c.GmailApp.sendEmail=()=>{throw Error('送信元拒否')};assert.throws(()=>rejected.c.nameJobNotify_('接続エラー',{}));assert.equal(fallback,0);assert.equal(rejected.props.NAME_JOB_NOTICE_接続エラー,undefined);
});
