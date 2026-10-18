/* GASの定期実行で、承認済み姓名補完をMacなしで続ける。 */
var NAME_JOB_BASE = 'https://crm-sfa-integration.vercel.app/api/contact-name-job';

function nameJobIdentity() {
  var token = ScriptApp.getIdentityToken();
  if (!token) throw new Error('Googleアカウントの実行許可が必要です');
  var payload = JSON.parse(Utilities.newBlob(Utilities.base64DecodeWebSafe(token.split('.')[1])).getDataAsString());
  // サーバの許可リスト用の公開識別子だけ。トークンは出力しない。
  console.log(JSON.stringify({audience: payload.aud, subject: payload.sub}));
}

function nameJobRequest_(path, method) {
  var token = ScriptApp.getIdentityToken();
  if (!token) throw new Error('実行者の認証がありません');
  return {url: NAME_JOB_BASE + path, method: method || 'get',
    headers: {Authorization: 'Bearer ' + token}, muteHttpExceptions: true, followRedirects: false};
}

function nameJobResult_(response) {
  if (response.getResponseCode() !== 200) throw new Error('CRM接続失敗 HTTP ' + response.getResponseCode());
  return JSON.parse(response.getContentText());
}

function nameJobStatus_() {
  var req = nameJobRequest_('/status');
  return nameJobResult_(UrlFetchApp.fetch(req.url, req));
}

function previewNameJob() {
  var status = nameJobStatus_();
  console.log(JSON.stringify(status));
  return status;
}

function startNameJob() {
  var status = nameJobStatus_();
  if (status.status !== 'running') throw new Error('CRM側で計画の登録・有効化を先に行ってください');
  stopNameJob();
  ScriptApp.newTrigger('nameJobTick').timeBased().everyMinutes(5).create();
  ScriptApp.newTrigger('nameJobWatchdog').timeBased().everyMinutes(15).create();
  PropertiesService.getScriptProperties().setProperty('NAME_JOB_STARTED_AT', String(Date.now()));
  nameJobTick();
}

function stopNameJob() {
  ScriptApp.getProjectTriggers().forEach(function(t) {
    if (['nameJobTick', 'nameJobWatchdog'].indexOf(t.getHandlerFunction()) !== -1) ScriptApp.deleteTrigger(t);
  });
}

function nameJobNotify_(kind, status) {
  var props = PropertiesService.getScriptProperties();
  var key = 'NAME_JOB_NOTICE_' + kind;
  var prior = Number(props.getProperty(key) || 0);
  if (Date.now() - prior < 3600000) return;
  var recipient = Session.getEffectiveUser().getEmail();
  if (!recipient) throw new Error('本人への通知先を取得できません');
  var counts = status.counts || {};
  var summary = '保存確認済み：' + (counts.verified || 0) + '件\n' +
    '未処理：' + (counts.pending || 0) + '件\n再試行待ち：' + (counts.retry || 0) + '件\n' +
    '入力変更による保留：' + (counts.held || 0) + '件\n繰り返し失敗：' + (counts.failed || 0) + '件\n';
  if (!status.counts) summary = '今回の接続では最新件数を取得できませんでした。\n';
  var next = kind === '完了' ? '全件の保存確認が完了し、定期実行を停止しました。' :
    (kind === '要確認' ? '自動処理を停止しました。保留・失敗の対象を確認してから再開してください。' :
    (kind === '一時停止' ? '定期実行を停止しました。CRM側の計画を有効化してから再開してください。' :
    '定期実行は継続します。進捗停止が続く場合は接続とGASの実行履歴を確認してください。'));
  MailApp.sendEmail(recipient, 'CRM姓名補完：' + kind,
    'GASからの実行結果です。\n' + summary + '\n' + next + '\n\n詳細：\n' + JSON.stringify(status, null, 2));
  props.setProperty(key, String(Date.now()));
}

function nameJobTick() {
  var lock = LockService.getScriptLock();
  if (!lock.tryLock(1000)) return;
  try {
    var props = PropertiesService.getScriptProperties();
    var before = nameJobStatus_();
    var shardErrors = 0;
    if (before.status === 'running') {
      var requests = [0, 1, 2, 3].map(function(shard) {
        return nameJobRequest_('/run?shard=' + shard, 'post');
      });
      var responses = UrlFetchApp.fetchAll(requests);
      responses.forEach(function(response) {
        try { nameJobResult_(response); } catch (error) { shardErrors += 1; }
      });
    }
    var after = nameJobStatus_();
    props.setProperty('NAME_JOB_LAST_TICK', String(Date.now()));
    props.setProperty('NAME_JOB_LAST_STATUS', JSON.stringify(after));
    console.log(JSON.stringify(after));
    if (after.status === 'completed' || after.status === 'needs_attention') {
      nameJobNotify_(after.status === 'completed' ? '完了' : '要確認', after);
      stopNameJob();
    } else if (after.status === 'paused' || after.status === 'not_imported') {
      nameJobNotify_('一時停止', after);
      stopNameJob();
    } else if (shardErrors) {
      nameJobNotify_('接続エラー', after);
    } else if (after.counts.failed > (before.counts.failed || 0) || after.counts.retry >= 20) {
      nameJobNotify_('保留・再試行', after);
    }
  } catch (error) {
    // HTTP本文や認証値を通知に含めない。
    nameJobNotify_('接続エラー', {message: '次の定期実行で再試行します'});
    throw new Error('姓名補完の実行に失敗しました。次回再試行します。');
  } finally { lock.releaseLock(); }
}

function nameJobWatchdog() {
  var props = PropertiesService.getScriptProperties();
  var tick = Number(props.getProperty('NAME_JOB_LAST_TICK') || props.getProperty('NAME_JOB_STARTED_AT') || 0);
  var triggers = ScriptApp.getProjectTriggers().filter(function(t) { return t.getHandlerFunction() === 'nameJobTick'; });
  if (triggers.length !== 1 || Date.now() - tick > 20 * 60000) {
    nameJobNotify_('定期処理停止', {lastTick: tick, triggerCount: triggers.length});
  }
  var status = nameJobStatus_();
  if (status.status === 'running' && (!status.last_success_at || Date.now() - Date.parse(status.last_success_at) > 30 * 60000)) {
    nameJobNotify_('進捗停止', status);
  }
}
