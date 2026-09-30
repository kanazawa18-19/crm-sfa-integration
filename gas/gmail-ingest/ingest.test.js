const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function runtime({failPost = false, failFirstMessage = false} = {}) {
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
  const context = {
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
      return {getResponseCode: () => failPost ? 500 : 200,
              getContentText: () => JSON.stringify({received: body.messages.length,
                results: body.messages.map(m => ({id: m.id,
                  status: failFirstMessage && calls.length === 1 ? 'effect_failed' : 'existing'}))})};
    }},
    ScriptApp: {getProjectTriggers: () => [], newTrigger: () => ({timeBased: () => ({everyMinutes: () => ({create() {}})})})},
    console: {log() {}},
  };
  vm.runInNewContext(fs.readFileSync(__dirname + '/ingest.js', 'utf8'), context);
  return {context, values, calls};
}

test('全ページ成功したときだけ取得位置を確定する', () => {
  const {context, values, calls} = runtime();
  context.gmailIngest();
  assert.equal(calls.length, 2);
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
  assert.equal(calls.length, 2);
  assert.equal(values.get('FAILED_MESSAGE_IDS'), '["m1"]');
  context.gmailIngest();
  assert.equal(calls[2].messages[0].id, 'm1');
  assert.equal(values.get('FAILED_MESSAGE_IDS'), '[]');
});
