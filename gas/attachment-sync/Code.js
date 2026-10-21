/* 添付同期専用。初期状態ではトリガー未登録。本番URLはプロパティで設定する。 */
function attachmentRequest_(path, method) {
  var base = PropertiesService.getScriptProperties().getProperty('ATTACHMENT_BASE_URL');
  if (!base || !/^https:\/\/[a-z0-9.-]+\/api\/attachment-sync$/.test(base)) throw new Error('添付APIのURLが未設定です');
  var token = ScriptApp.getIdentityToken();
  if (!token) throw new Error('本人の認証がありません');
  var response = UrlFetchApp.fetch(base + path, {method: method || 'get', headers: {Authorization: 'Bearer ' + token}, followRedirects: false, muteHttpExceptions: true});
  if (response.getResponseCode() === 409) throw new Error(JSON.parse(response.getContentText()).detail);
  if (response.getResponseCode() !== 200) throw new Error('添付API接続失敗 HTTP ' + response.getResponseCode());
  return JSON.parse(response.getContentText());
}
function attachmentIdentity() {
  var value = JSON.parse(Utilities.newBlob(Utilities.base64DecodeWebSafe(ScriptApp.getIdentityToken().split('.')[1])).getDataAsString());
  console.log(JSON.stringify({audience: value.aud, subject: value.sub}));
}
function previewAttachments() { var result = attachmentRequest_('/preview', 'post'); console.log(JSON.stringify(result)); return result; }
function attachmentStatus() { var result = attachmentRequest_('/status'); console.log(JSON.stringify(result)); return result; }
function stopAttachments() {
  ScriptApp.getProjectTriggers().forEach(function(t) { if (['attachmentTick', 'attachmentWatchdog'].indexOf(t.getHandlerFunction()) >= 0) ScriptApp.deleteTrigger(t); });
}
function startAttachments() {
  if (!attachmentStatus().enabled) throw new Error('CRM側の添付同期が停止中です');
  stopAttachments();
  ScriptApp.newTrigger('attachmentTick').timeBased().everyMinutes(5).create();
  ScriptApp.newTrigger('attachmentWatchdog').timeBased().everyMinutes(15).create();
  PropertiesService.getScriptProperties().setProperty('ATTACHMENT_STARTED', String(Date.now()));
}
function attachmentNotice_(key, subject, body, hours) {
  var props = PropertiesService.getScriptProperties();
  if (Date.now() - Number(props.getProperty(key) || 0) < hours * 3600000) return;
  var recipient = Session.getEffectiveUser().getEmail();
  if (!recipient) throw new Error('通知先がありません');
  MailApp.sendEmail(recipient, subject, body);
  props.setProperty(key, String(Date.now()));
}
function attachmentNeedsAttention_(status) {
  var counts = status.counts || {};
  return !status.enabled || !!(counts.held || counts.retry || status.discovery_error_count);
}
function attachmentTick() {
  var lock = LockService.getScriptLock();
  if (!lock.tryLock(1000)) return;
  var start = Date.now();
  try {
    var props = PropertiesService.getScriptProperties();
    props.setProperty('ATTACHMENT_MONITOR_SNAPSHOT', '');
    var result = attachmentRequest_('/run', 'post');
    props.setProperty('ATTACHMENT_DURATION', String(Date.now() - start));
    if (['verified', 'mirrored', 'idle'].indexOf(result.status) >= 0) props.setProperty('ATTACHMENT_SUCCESS', String(Date.now()));
    var status = attachmentRequest_('/status');
    props.setProperty('ATTACHMENT_MONITOR_SNAPSHOT', JSON.stringify({
      checkedAt: Date.now(), durationMs: Date.now() - start, resultStatus: result.status, status: status
    }));
    if (status.status === 'busy') return;
    if (attachmentNeedsAttention_(status)) attachmentNotice_('ATTACHMENT_ALERT', 'CRM添付：要確認', '保留・再試行・巡回失敗があります。件数：' + JSON.stringify(status.counts || {}) + '、巡回失敗：' + (status.discovery_error_count || 0), 4);
    else attachmentNotice_('ATTACHMENT_SUMMARY', 'CRM添付：定期確認', '配送確認済み・保留・未処理の件数：' + JSON.stringify(status.counts || {}) + '。未処理はまだ配送されていません。', 24);
  } catch (error) {
    attachmentNotice_('ATTACHMENT_ALERT', 'CRM添付：接続失敗', '添付の同期に失敗しました。実行履歴を確認してください。', 4);
    throw new Error('添付同期に失敗しました');
  } finally { lock.releaseLock(); }
}
function attachmentWatchdog() {
  var props = PropertiesService.getScriptProperties();
  var last = Number(props.getProperty('ATTACHMENT_SUCCESS') || props.getProperty('ATTACHMENT_STARTED') || 0);
  var ticks = ScriptApp.getProjectTriggers().filter(function(t) { return t.getHandlerFunction() === 'attachmentTick'; });
  try {
    var now = Date.now();
    var stopped = ticks.length !== 1 || !isFinite(last) || last <= 0 || last > now || now - last > 30 * 60000;
    var snapshot;
    try { snapshot = JSON.parse(props.getProperty('ATTACHMENT_MONITOR_SNAPSHOT') || 'null'); } catch (_) { snapshot = null; }
    var fresh = snapshot && typeof snapshot.checkedAt === 'number' && isFinite(snapshot.checkedAt) &&
      snapshot.checkedAt > 0 && snapshot.checkedAt <= now && now - snapshot.checkedAt <= 20 * 60000;
    var durationValid = fresh && typeof snapshot.durationMs === 'number' && isFinite(snapshot.durationMs) && snapshot.durationMs >= 0;
    var slow = durationValid && snapshot.durationMs > 240000;
    var reusable = !stopped && durationValid && !slow && ['verified', 'mirrored', 'idle'].indexOf(snapshot.resultStatus) >= 0 && snapshot.status && snapshot.status.enabled === true &&
      snapshot.status.counts && snapshot.status.status !== 'busy' && !attachmentNeedsAttention_(snapshot.status);
    var status = reusable ? snapshot.status : attachmentRequest_('/status');
    if (stopped || slow || (status.status !== 'busy' && attachmentNeedsAttention_(status)))
      attachmentNotice_('ATTACHMENT_ALERT', 'CRM添付：監視異常', '定期処理の停止・遅延・配送保留があります。attachmentProblems と実行履歴を確認してください。', 4);
  } catch (error) {
    attachmentNotice_('ATTACHMENT_ALERT', 'CRM添付：監視接続失敗', '監視から添付APIへ接続できませんでした。', 4);
    throw new Error('添付の監視に失敗しました');
  }
}
function attachmentProblems() { var offset = Number(PropertiesService.getScriptProperties().getProperty('ATTACHMENT_PROBLEMS_OFFSET') || 0);
  if (!Number.isInteger(offset) || offset < 0) throw new Error('一覧の開始位置が不正です');
  var result = attachmentRequest_('/problems?offset=' + offset); console.log(JSON.stringify(result)); return result; }
function retryAttachment() {
  var id = PropertiesService.getScriptProperties().getProperty('ATTACHMENT_RETRY_ID') || '';
  if (!/^[a-f0-9]{64}$/.test(id)) throw new Error('再試行する添付IDを ATTACHMENT_RETRY_ID に指定してください');
  var result = attachmentRequest_('/retry/' + id, 'post'); console.log(JSON.stringify(result)); return result;
}
