const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
function setup(status, result = {status:'idle'}) {
  const props = new Map([['ATTACHMENT_BASE_URL','https://example.invalid/api/attachment-sync']]);
  const mail=[], triggers=[], calls=[];
  const ctx = {console:{log(){}}, Date, JSON, Error,
    PropertiesService:{getScriptProperties:()=>({getProperty:k=>props.get(k),setProperty:(k,v)=>props.set(k,v)})},
    ScriptApp:{getIdentityToken:()=> 'synthetic',getProjectTriggers:()=>triggers.slice(),deleteTrigger:t=>triggers.splice(triggers.indexOf(t),1),newTrigger:name=>({timeBased:()=>({everyMinutes:n=>({create:()=>triggers.push({getHandlerFunction:()=>name,minutes:n})})})})},
    LockService:{getScriptLock:()=>({tryLock:()=>true,releaseLock:()=>calls.push('released')})},
    Session:{getEffectiveUser:()=>({getEmail:()=> 'synthetic@example.invalid'})},
    MailApp:{sendEmail:(...args)=>mail.push(args)},
    UrlFetchApp:{fetch:(url,opts)=>{calls.push(url);assert.equal(opts.followRedirects,false);return {getResponseCode:()=>200,getContentText:()=>JSON.stringify(url.endsWith('/run')?result:status)}}}
  };
  vm.createContext(ctx);vm.runInContext(fs.readFileSync(__dirname+'/Code.js','utf8'),ctx);
  return {ctx,props,mail,triggers,calls};
}
test('停止中にスケジュールを登録しない',()=>{
  const s=setup({enabled:false});assert.throws(()=>s.ctx.startAttachments());assert.equal(s.triggers.length,0);
});
test('開始を繰り返しても5分処理と監視を二重登録しない',()=>{
  const s=setup({enabled:true});s.ctx.startAttachments();s.ctx.startAttachments();assert.equal(s.triggers.length,2);assert.deepEqual(s.triggers.map(t=>t.minutes),[5,15]);
});
test('busyを配送成功と記録しない',()=>{
  const s=setup({status:'busy'},{status:'busy'});s.ctx.attachmentTick();assert.equal(s.props.get('ATTACHMENT_SUCCESS'),undefined);assert.equal(s.mail.length,0);assert.equal(s.calls.at(-1),'released');
});
test('保留通知は理由のある件数を送り4時間抑制',()=>{
  const s=setup({enabled:true,counts:{held:1}},{status:'held'});s.ctx.attachmentTick();s.ctx.attachmentTick();assert.equal(s.mail.length,1);assert.match(s.mail[0][1],/要確認/);assert.equal(s.props.get('ATTACHMENT_SUCCESS'),undefined);
});
test('通常処理は最終正常時刻と日次件数を記録',()=>{
  const s=setup({enabled:true,counts:{verified:1}});s.ctx.attachmentTick();s.ctx.attachmentTick();assert.ok(s.props.get('ATTACHMENT_SUCCESS'));assert.equal(s.mail.length,1);assert.match(s.mail[0][1],/定期確認/);
});
test('トリガー消失を監視で通知',()=>{
  const s=setup({enabled:true,counts:{}});s.ctx.attachmentWatchdog();assert.equal(s.mail.length,1);assert.match(s.mail[0][1],/監視異常/);
});
test('既存の複製と確認できた処理も正常終了として監視に反映',()=>{
  const s=setup({enabled:true,counts:{mirrored:1}},{status:'mirrored'});s.ctx.attachmentTick();assert.ok(s.props.get('ATTACHMENT_SUCCESS'));assert.match(s.mail[0][1],/定期確認/);
});
