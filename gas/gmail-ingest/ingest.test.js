const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function runtime({failPost = false, failFirstMessage = false, failTerminal = false} = {}) {
  const values = new Map([
    ['CRM_INGEST_URL', 'https://example.invalid/api/webhooks/gmail-ingest'],
    ['CRM_INGEST_SECRET', 'test-secret'], ['REP_EMAIL', 'rep@example.com'],
    ['DRY_RUN', 'true'],
  ]);
  const calls = [];
  const props = {
    getProperty: key => values.get(key),
    setProperty: (key, value) => values.set(key, value),
    deleteProperty: key => values.delete(key),
  };
  const logs = [];
  const context = {
    Utilities: {getUuid: () => 'test-scan-identifier-0001'},
    PropertiesService: {getScriptProperties: () => props},
    LockService: {getScriptLock: () => ({tryLock: () => true, releaseLock() {}})},
    Gmail: {Users: {Messages: {
      list: (_user, options) => options.pageToken
        ? {messages: [{id: 'm2'}]} : {messages: [{id: 'm1'}], nextPageToken: 'page-2'},
      get: (_user, id) => ({id, internalDate: '1780000000000',
        payload: {headers: [{name: 'From', value: 'buyer@customer.com'},
                            {name: 'To', value: 'rep@example.com'}]}}),
    }}},
    UrlFetchApp: {fetch: (_url, options) => {
      const body = JSON.parse(options.payload);
      calls.push(body);
      return {getResponseCode: () => failPost || (failTerminal && body.observation.phase === 'complete') ? 500 : 200,
              getContentText: () => JSON.stringify({observation_saved: true, received: body.messages.length,
                results: body.messages.map(m => ({id: m.id,
                  status: failFirstMessage && calls.length === 2 ? 'effect_failed' : 'existing'}))})};
    }},
    ScriptApp: {getProjectTriggers: () => [], newTrigger: () => ({timeBased: () => ({everyMinutes: () => ({create() {}})})})},
    console: {log(value) {logs.push(JSON.parse(value));}},
  };
  vm.runInNewContext(fs.readFileSync(__dirname + '/ingest.js', 'utf8'), context);
  return {context, values, calls, logs};
}

test('全ページ成功したときだけ取得位置を確定する', () => {
  const {context, values, calls} = runtime();
  context.gmailIngest();
  assert.equal(calls.length, 4);
  assert.ok(values.get('LAST_DRY_RUN_SUCCESS_SECONDS'));
  assert.equal(values.has('INGEST_CURSOR'), false);
  assert.equal(values.has('LAST_SUCCESS_SECONDS'), false);
});

test('HTTP失敗時は成功位置を進めない', () => {
  const {context, values} = runtime({failPost: true});
  assert.throws(() => context.gmailIngest(), /HTTP 500/);
  assert.equal(values.has('LAST_DRY_RUN_SUCCESS_SECONDS'), false);
});

test('1通の後処理失敗を保持し、次の実行で再試行する', () => {
  const {context, values, calls} = runtime({failFirstMessage: true});
  values.set('DRY_RUN', 'false');
  assert.throws(() => context.gmailIngest(), /prepareGmailIngestLive/);
  context.prepareGmailIngestLive();
  context.gmailIngest();
  assert.equal(calls.length, 4);
  assert.equal(values.get('FAILED_MESSAGE_IDS'), '["m1"]');
  context.Utilities.getUuid = () => 'test-scan-identifier-0002';
  context.gmailIngest();
  assert.equal(calls.find(call => call.observation.phase === 'retry').messages[0].id, 'm1');
  assert.equal(values.get('FAILED_MESSAGE_IDS'), '[]');
});


test('走査完了証跡は全ページ成功時だけ範囲付きで出す', () => {
  const {context, logs} = runtime();
  context.gmailIngest();
  const completed = logs.filter(row => row.scanComplete);
  assert.equal(completed.length, 1);
  const batches = logs.filter(row => row.results);
  assert.ok(batches.length > 1);
  assert.ok(batches.every(row => row.after === completed[0].after && row.before === completed[0].before));
  assert.equal(completed[0].dryRun, true);
  const failed = runtime({failPost: true});
  assert.throws(() => failed.context.gmailIngest());
  assert.equal(failed.logs.some(row => row.scanComplete), false);
});

test('時間切れ後も元の範囲と次ページを引き継いで完走する', () => {
  const {context, values, calls, logs} = runtime();
  const start = 1791800000000;
  let ticks = 0;
  context.Date = class extends Date {
    static now() { return start + (++ticks > 2 ? 241000 : 0); }
  };
  context.gmailIngest();
  const cursor = JSON.parse(values.get('INGEST_CURSOR'));
  assert.equal(cursor.pageToken, 'page-2');
  assert.equal(values.has('LAST_DRY_RUN_SUCCESS_SECONDS'), false);
  assert.equal(logs.some(row => row.scanComplete), false);
  context.gmailIngest();
  assert.deepEqual(calls.filter(call => call.observation.phase === 'batch').map(call => call.messages[0].id), ['m1', 'm2']);
  const completed = logs.find(row => row.scanComplete);
  assert.equal(completed.after, cursor.after);
  assert.equal(completed.before, cursor.before);
  assert.equal(values.get('LAST_DRY_RUN_SUCCESS_SECONDS'), String(cursor.before - 1));
  assert.equal(values.has('INGEST_CURSOR'), false);
});

 test('終端保存に失敗したら成功位置を維持し次回は終端だけ再送する', () => {
  const {context, values, calls} = runtime({failTerminal: true});
  assert.throws(() => context.gmailIngest(), /HTTP 500/);
  assert.equal(values.has('LAST_DRY_RUN_SUCCESS_SECONDS'), false);
  assert.equal(JSON.parse(values.get('INGEST_CURSOR')).terminalPending, true);
  assert.throws(() => context.gmailIngest(), /HTTP 500/);
  assert.equal(calls.filter(c => c.observation.phase === 'batch').length, 2);
});

test('応答紛失後のバッチは同じ走査IDと連番とメールIDで再送する', () => {
  const {context, values, calls} = runtime();
  const fetch = context.UrlFetchApp.fetch;
  let lost = false;
  context.UrlFetchApp.fetch = (url, options) => {
    const response = fetch(url, options);
    if (!lost && JSON.parse(options.payload).observation.phase === 'batch') {
      lost = true;
      throw new Error('応答紛失');
    }
    return response;
  };
  assert.throws(() => context.gmailIngest(), /応答紛失/);
  context.gmailIngest();
  const batches = calls.filter(c => c.observation.phase === 'batch');
  assert.deepEqual(batches[0], batches[1]);
  assert.equal(values.has('INGEST_CURSOR'), false);
});

test('固定済みIDの確定消失は未完了走査を残し成功位置から再開する', () => {
 const {context, values, calls}=runtime();
 context.Gmail.Users.Messages.get=()=>{const error=new Error('Requested entity was not found.');error.code=404;throw error;};
 assert.throws(()=>context.gmailIngest(),/消失/);
 assert.equal(values.has('INGEST_CURSOR'),false);
 assert.equal(values.has('LAST_DRY_RUN_SUCCESS_SECONDS'),false);
 assert.equal(calls.find(c=>c.observation.phase==='missing').observation.missing_ids[0],'m1');
 assert.equal(calls.some(c=>c.observation.phase==='complete'),false);
 context.Utilities.getUuid=()=> 'new-scan-identifier-0002';
 context.Gmail.Users.Messages.list=()=>({messages:[]});
 context.gmailIngest();
 assert.equal(calls.at(-1).observation.scan_id,'new-scan-identifier-0002');
 assert.ok(values.get('LAST_DRY_RUN_SUCCESS_SECONDS'));
});

test('再試行IDの消失は失敗列から欠落記録へ移し、一時障害は固定IDを維持する', () => {
 const {context, values, calls}=runtime();values.set('DRY_RUN','false');context.prepareGmailIngestLive();
 values.set('FAILED_MESSAGE_IDS','["gone"]');
 context.Gmail.Users.Messages.get=()=>{throw new Error('503 temporarily unavailable');};
 assert.throws(()=>context.gmailIngest(),/503/);
 assert.ok(values.has('INGEST_CURSOR'));assert.equal(values.get('FAILED_MESSAGE_IDS'),'["gone"]');
 context.Gmail.Users.Messages.get=()=>{throw new Error('404 not found');};
 assert.throws(()=>context.gmailIngest(),/消失/);
 assert.equal(values.has('INGEST_CURSOR'),false);assert.equal(values.get('FAILED_MESSAGE_IDS'),'[]');
 assert.equal(calls.find(c=>c.observation.phase==='missing').observation.missing_ids[0],'gone');
});

test('長期停止後は31日以下に分割し前の成功位置から連続して進める', () => {
 const {context, values, calls}=runtime();
 const now=Math.floor(Date.now()/1000);
 values.set('LAST_DRY_RUN_SUCCESS_SECONDS',String(now-100*86400));
 context.Gmail.Users.Messages.list=()=>({messages:[]});
 context.gmailIngest();
 const first=calls.find(c=>c.observation.phase==='complete').observation;
 assert.equal(first.before-first.after,31*86400);
 context.Utilities.getUuid=()=> 'new-scan-identifier-0002';context.gmailIngest();
 const second=calls.at(-1).observation;
 assert.equal(second.after,first.before-1-7200);
 assert.ok(second.before>first.before);assert.ok(second.before-second.after<=31*86400);
});

test('消失証跡の保存に失敗したら固定IDを維持し次回同じ証跡を再送する', () => {
 const {context, values, calls}=runtime();
 const fetch=context.UrlFetchApp.fetch;
 context.Gmail.Users.Messages.get=()=>{throw new Error('404 not found');};
 context.UrlFetchApp.fetch=(url,options)=>JSON.parse(options.payload).observation.phase==='missing'
   ? {getResponseCode:()=>500} : fetch(url,options);
 assert.throws(()=>context.gmailIngest(),/HTTP 500/);
 const before=values.get('INGEST_CURSOR');assert.ok(before);
 context.UrlFetchApp.fetch=fetch;
 assert.throws(()=>context.gmailIngest(),/消失/);
 assert.equal(calls.at(-1).observation.scan_id,JSON.parse(before).scanId);
 context.Utilities.getUuid=()=> 'new-scan-identifier-0002';
 // 一覧に同じ消失IDが再出現しても、DB記録済みのIDは再取得せず進める。
 context.Gmail.Users.Messages.list=()=>({messages:[{id:'m1'}]});
 context.gmailIngest();
 assert.equal(values.has('INGEST_CURSOR'),false);
 assert.ok(values.get('LAST_DRY_RUN_SUCCESS_SECONDS'));
});

test('消失通知の応答紛失後に先行IDも消えたら同じ走査で新しいIDを通知する', () => {
 const {context,values,calls}=runtime();
 const get=context.Gmail.Users.Messages.get, fetch=context.UrlFetchApp.fetch;
 context.Gmail.Users.Messages.list=()=>({messages:[{id:'m1'},{id:'m2'}]});
 context.Gmail.Users.Messages.get=(user,id)=>{if(id==='m2')throw new Error('404 not found');return get(user,id);};
 context.UrlFetchApp.fetch=(url,options)=>{
   const response=fetch(url,options);
   if(JSON.parse(options.payload).observation.phase==='missing')throw new Error('ack lost');
   return response;
 };
 assert.throws(()=>context.gmailIngest(),/ack lost/);
 const scan=JSON.parse(values.get('INGEST_CURSOR')).scanId;
 context.UrlFetchApp.fetch=fetch;
 context.Gmail.Users.Messages.get=()=>{throw new Error('404 not found');};
 assert.throws(()=>context.gmailIngest(),/消失/);
 const missing=calls.filter(c=>c.observation.phase==='missing').map(c=>c.observation);
 assert.deepEqual(missing.map(o=>o.missing_ids[0]),['m2','m1']);
 assert.ok(missing.every(o=>o.scan_id===scan&&o.sequence===0));
 assert.equal(values.has('INGEST_CURSOR'),false);
});

test('照合ONは本文・CC・共通IDを取得し添付を除外する', () => {
  const {context, values} = runtime();
  values.set('EMAIL_REVIEW_ENABLED', 'true');
  context.Utilities.base64DecodeWebSafe = value => Buffer.from(value, 'base64url');
  context.Utilities.newBlob = bytes => ({getDataAsString: () => bytes.toString('utf8')});
  const plain = (text, extra = {}) => ({mimeType: 'text/plain', body: {data: Buffer.from(text).toString('base64url')}, ...extra});
  context.Gmail.Users.Messages.get = (_user, id, options) => {
    assert.equal(options.format, 'full');
    return {id, payload: {mimeType: 'multipart/mixed', headers: [{name: 'Cc', value: 'cc@example.test'}, {name: 'Message-ID', value: '<mail@example.test>'}],
      parts: [plain('本文'), plain('添付', {filename: 'a.txt'}), plain('転送', {mimeType: 'message/rfc822'})]}};
  };
  const result = context.gmailIngestMessage('m');
  assert.equal(result.body_text, '本文');
  assert.equal(result.cc_header, 'cc@example.test');
  assert.equal(result.message_id_header, '<mail@example.test>');
  assert.equal(context.gmailReviewPlainText(plain('x'.repeat(20000))).length, 20000);
  assert.equal(context.gmailReviewPlainText(plain('x'.repeat(20001))), '');
  assert.equal(context.gmailReviewPlainText({mimeType: 'multipart/mixed', parts: [plain('本文'), plain('x'.repeat(20001))]}), '');
  assert.equal(context.gmailReviewPlainText({mimeType: 'multipart/mixed', parts: Array(31).fill(plain('本文'))}), '');
  assert.equal(context.gmailReviewPlainText(plain('<b>html</b>', {mimeType: 'text/html'})), '');
  for (const marker of ['2026年10月16日(金) 14:50 担当 <person@example.test>:',
    'On Fri, 16 Oct 2026 at 14:50 Someone wrote:', '担当様は書きました：',
    '-----Original Message-----', '---------- Forwarded message ---------',
    'From: person@example.test\r\nSent: Friday\r\nTo: me@example.test\r\nSubject: 前回']) {
    const body = '今回の本文\r\n\r\n-- \r\n署名';
    assert.equal(context.gmailReviewPlainText(plain(body + '\r\n\r\n' + marker + '\r\n' + '過去'.repeat(15000))), body);
  }
  for (const body of ['今回の本文\r\n\r\n---\r\n署名\r\n', 'From: の項目を設定してください\n日付は2026年10月16日です。\n']) {
    assert.equal(context.gmailReviewCurrentBody(body), body);
  }
  assert.equal(context.gmailReviewCurrentBody('> 質問\n回答\n> 別の質問\n別の回答'), '回答\n別の回答');
  assert.equal(context.gmailReviewCurrentBody('On Friday, A wrote:\n> 質問\n回答\n--\n署名'), '回答\n--\n署名');
});
