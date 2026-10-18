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
    MailApp:{sendEmail:(...a)=>sent.push(a)},
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
  h.c.MailApp.sendEmail=()=>{throw new Error('メール利用枠不足');};
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
