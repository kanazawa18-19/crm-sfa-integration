/* CRM専用の外部監視。業務レコード・購読設定は変更しない。 */
function webhookHealthTick() { return runWebhookHealth_(false); }
function previewWebhookHealth() { return runWebhookHealth_(true); }

function runWebhookHealth_(preview) {
  var lock = LockService.getScriptLock();
  if (!lock.tryLock(1000)) return {status: '実行中のため保留'};
  try {
    var props = PropertiesService.getScriptProperties();
    var token = props.getProperty('WEBHOOK_HEALTH_TOKEN');
    var slackToken = props.getProperty('SLACK_BOT_TOKEN');
    var channel = props.getProperty('SLACK_DM_CHANNEL');
    if (!token || !slackToken || !channel || channel.charAt(0) !== 'D') throw new Error('監視用設定が不足しています');
    var now = Date.now();
    var checks;
    try {
      var response = UrlFetchApp.fetch('https://crm-sfa-integration.vercel.app/api/diagnostics/webhook-health', {
        headers: {Authorization: 'Bearer ' + token}, muteHttpExceptions: true, followRedirects: false
      });
      if (response.getResponseCode() !== 200) throw new Error('診断APIに到達できません');
      var report = WebhookHealth.validate(JSON.parse(response.getContentText()), Date.now());
      checks = report.checks.concat([{key: 'monitor', status: 'ok', reason: '診断APIへ到達・応答内容を確認'}]);
    } catch (_) {
      checks = [{key: 'monitor', status: 'unknown', reason: '診断APIの到達・認証・応答を確認できません'}];
    }
    var previous = JSON.parse(props.getProperty('WEBHOOK_HEALTH_STATE') || '{"checks":{}}');
    var plan = WebhookHealth.plan(checks, previous, now);
    var messages = [];
    if (plan.events.length) messages.push('【CRM連携監視：状態変化】\n' + plan.events.map(function (e) { return (e.level === 'recovered' ? '復旧' : e.level === 'critical' ? '異常' : e.level === 'unknown' ? '監視不能' : '要確認') + '｜' + e.text; }).join('\n'));
    if (plan.daily) messages.push('【CRM連携監視：日次報告】\n監視を実行しました（JST ' + Utilities.formatDate(new Date(now), 'Asia/Tokyo', 'yyyy/MM/dd HH:mm') + '）\n' + plan.lines.join('\n') + (checks.length === 1 ? '\nNotion・Zoho・kintone・シート：今回の状態は確認不能（前回の通知状態を保持）' : '') + '\n受信記録は同期成功の保証ではありません。一部DB・アプリだけの停止は検出対象外。シート受信なしは停止と断定しません。\n手順：crm-sfa-integration/docs/webhook_health_operations.md');
    if (preview) { console.log(JSON.stringify({preview: true, messages: messages})); return {preview: true, messages: messages}; }
    // 全送信がSlackに受理された後だけ通知済みを確定。失敗時は次回再送する。
    messages.forEach(function (text) { sendWebhookHealthSlack_(slackToken, channel, text); });
    props.setProperty('WEBHOOK_HEALTH_STATE', JSON.stringify(plan.next));
    props.setProperty('WEBHOOK_HEALTH_LAST_RUN', new Date(now).toISOString());
    console.log(JSON.stringify({status: '完了', messages: messages.length, checks: checks.length, elapsedMs: Date.now() - now}));
    return {status: '完了', messages: messages.length, checks: checks.length};
  } finally { lock.releaseLock(); }
}

function sendWebhookHealthSlack_(token, channel, text) {
  var response = UrlFetchApp.fetch('https://slack.com/api/chat.postMessage', {
    method: 'post', contentType: 'application/json', headers: {Authorization: 'Bearer ' + token},
    payload: JSON.stringify({channel: channel, text: text, unfurl_links: false, unfurl_media: false}),
    muteHttpExceptions: true, followRedirects: false
  });
  var body;
  try { body = JSON.parse(response.getContentText()); } catch (_) { throw new Error('Slack応答形式が不正です'); }
  if (response.getResponseCode() !== 200 || !body.ok || body.channel !== channel || !body.ts) throw new Error('Slack通知が受理されませんでした');
}

function installWebhookHealthMonitor() {
  var lock = LockService.getScriptLock(); lock.waitLock(5000);
  try {
    var own = ScriptApp.getProjectTriggers().filter(function (t) { return t.getHandlerFunction() === 'webhookHealthTick'; });
    if (own.length > 1) throw new Error('監視トリガーが重複しています');
    if (!own.length) ScriptApp.newTrigger('webhookHealthTick').timeBased().everyHours(1).create();
    console.log(JSON.stringify(webhookHealthStatus()));
  } finally { lock.releaseLock(); }
}

function webhookHealthStatus() {
  var props = PropertiesService.getScriptProperties();
  var status = {triggers: ScriptApp.getProjectTriggers().filter(function (t) { return t.getHandlerFunction() === 'webhookHealthTick'; }).length,
    lastRun: props.getProperty('WEBHOOK_HEALTH_LAST_RUN'), configured: ['WEBHOOK_HEALTH_TOKEN', 'SLACK_BOT_TOKEN', 'SLACK_DM_CHANNEL'].every(function (k) { return !!props.getProperty(k); })};
  console.log(JSON.stringify(status)); return status;
}
