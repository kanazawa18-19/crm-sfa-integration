/* 未所属の連絡先を毎時照合する。姓名補完とは独立した専用プロジェクト。 */
var AFFILIATION_JOB_BASE = 'https://crm-sfa-integration.vercel.app/api/contact-affiliation-job';

function affiliationJobIdentity() {
  var token = ScriptApp.getIdentityToken();
  if (!token) throw new Error('実行者の認証がありません');
  var value = JSON.parse(Utilities.newBlob(Utilities.base64DecodeWebSafe(token.split('.')[1])).getDataAsString());
  console.log(JSON.stringify({audience: value.aud, subject: value.sub}));
}

function affiliationJobRequest_(path, method) {
  var token = ScriptApp.getIdentityToken();
  if (!token) throw new Error('実行者の認証がありません');
  var response = UrlFetchApp.fetch(AFFILIATION_JOB_BASE + path, {
    method: method || 'get', headers: {Authorization: 'Bearer ' + token},
    muteHttpExceptions: true, followRedirects: false
  });
  if (response.getResponseCode() !== 200) throw new Error('CRM接続失敗 HTTP ' + response.getResponseCode());
  return JSON.parse(response.getContentText());
}

function previewAffiliationJob() {
  var status = affiliationJobRequest_('/status');
  console.log(JSON.stringify(status));
  return status;
}

function stopAffiliationJob() {
  ScriptApp.getProjectTriggers().forEach(function(trigger) {
    if (['affiliationJobTick', 'affiliationJobWatchdog'].indexOf(trigger.getHandlerFunction()) >= 0) ScriptApp.deleteTrigger(trigger);
  });
}

function startAffiliationJob() {
  if (!previewAffiliationJob().enabled) throw new Error('CRM側で試算または実行を有効にしてください');
  stopAffiliationJob();
  ScriptApp.newTrigger('affiliationJobTick').timeBased().everyHours(1).create();
  ScriptApp.newTrigger('affiliationJobWatchdog').timeBased().everyMinutes(15).create();
  PropertiesService.getScriptProperties().setProperty('AFFILIATION_STARTED_AT', String(Date.now()));
  affiliationJobTick();
}

function affiliationJobNotify_(message) {
  var props = PropertiesService.getScriptProperties();
  if (Date.now() - Number(props.getProperty('AFFILIATION_LAST_NOTICE') || 0) < 4 * 3600000) return;
  var recipient = Session.getEffectiveUser().getEmail();
  if (!recipient) throw new Error('本人への通知先を取得できません');
  MailApp.sendEmail(recipient, 'CRM所属照合：要確認', message);
  props.setProperty('AFFILIATION_LAST_NOTICE', String(Date.now()));
}

function affiliationJobTick() {
  var lock = LockService.getScriptLock();
  if (!lock.tryLock(1000)) return;
  try {
    var started = Date.now();
    var result = {status: 'ok', counts: {}, discovered: 0, duration_ms: 0};
    for (var batch = 0; batch < 4 && Date.now() - started < 180000; batch++) {
      var chunk = affiliationJobRequest_('/run', 'post');
      result.status = chunk.status;
      if (chunk.status !== 'ok') break;
      Object.keys(chunk.counts || {}).forEach(function(key) { result.counts[key] = (result.counts[key] || 0) + chunk.counts[key]; });
      result.discovered += chunk.discovered || 0;
      result.duration_ms += chunk.duration_ms || 0;
    }
    PropertiesService.getScriptProperties().setProperty('AFFILIATION_LAST_DURATION', String(Date.now() - started));
    PropertiesService.getScriptProperties().setProperty('AFFILIATION_LAST_TICK', String(Date.now()));
    console.log(JSON.stringify(result));
    if (result.status === 'paused') affiliationJobNotify_('CRM側で一時停止しています。');
    var status = affiliationJobRequest_('/status');
    if (status.last_error || status.retry_count || (status.counts || {}).delivery_pending || (status.counts || {}).writing) {
      affiliationJobNotify_('保存・配送の確認待ちがあります。' + JSON.stringify(status));
    }
    if (result.status === 'ok') { affiliationJobSummary_(status, result); }
  } catch (error) {
    affiliationJobNotify_('接続または照合に失敗しました。次の定期実行で再試行します。');
    throw new Error('所属照合に失敗しました。');
  } finally { lock.releaseLock(); }
}

function affiliationJobWatchdog() {
  var props = PropertiesService.getScriptProperties();
  var tick = Number(props.getProperty('AFFILIATION_LAST_TICK') || props.getProperty('AFFILIATION_STARTED_AT') || 0);
  var triggers = ScriptApp.getProjectTriggers().filter(function(t) { return t.getHandlerFunction() === 'affiliationJobTick'; });
  try {
    var status = affiliationJobRequest_('/status');
    if (triggers.length !== 1 || Date.now() - tick > 90 * 60000 || status.last_error || status.retry_count ||
        Number(props.getProperty('AFFILIATION_LAST_DURATION') || 0) > 240000 ||
        (status.counts || {}).delivery_pending || (status.counts || {}).writing ||
        (status.enabled && (!status.last_success_at || Date.now() - Date.parse(status.last_success_at) > 90 * 60000))) {
      affiliationJobNotify_('定期処理の停止・遅延または照合失敗を検出しました。GAS実行履歴とCRM状態を確認してください。');
    }
  } catch (error) {
    affiliationJobNotify_('監視からCRMに接続できませんでした。');
    throw new Error('所属照合の監視に失敗しました。');
  }
}


function affiliationJobSummary_(status, result) {
  var props = PropertiesService.getScriptProperties();
  if (Date.now() - Number(props.getProperty('AFFILIATION_LAST_SUMMARY') || 0) < 24 * 3600000) return;
  var recipient = Session.getEffectiveUser().getEmail();
  if (!recipient) throw new Error('本人への通知先を取得できません');
  MailApp.sendEmail(recipient, 'CRM所属照合：定期処理の結果',
    '試算モード：' + (status.dry_run ? '試算のみ' : '保存あり') + '\n' + affiliationJobCounts_(status.counts) +
    '\n今回の処理：\n' + affiliationJobCounts_(result.counts) + '\n今回の巡回件数：' + result.discovered +
    '\n所要時間：' + result.duration_ms + 'ms\n保留は未確定です。成功件数には含めません。');
  props.setProperty('AFFILIATION_LAST_SUMMARY', String(Date.now()));
}


function affiliationJobCounts_(counts) {
  var labels = {pending: '未評価', candidate: '試算で一致', held: '根拠不足・判断保留',
    existing: '既存の所属あり', released: '所属解除後の保留', writing: '保存結果確認待ち', delivery_pending: '配送確認待ち', verified: '保存・配送確認済み'};
  return Object.keys(labels).map(function(key) { return labels[key] + '：' + ((counts || {})[key] || 0) + '件'; }).join('\n');
}
