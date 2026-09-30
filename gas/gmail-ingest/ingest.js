/** Gmail取得だけを担当し、判定・保存はCRM側へ渡す。 */
function gmailIngest() {
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(0)) return;
  try {
    const props = PropertiesService.getScriptProperties();
    const url = props.getProperty('CRM_INGEST_URL');
    const secret = props.getProperty('CRM_INGEST_SECRET');
    const repEmail = props.getProperty('REP_EMAIL');
    if (!url || !secret || !repEmail) throw new Error('GASの設定が不足しています');
    const dryRun = props.getProperty('DRY_RUN') !== 'false';
    if (!dryRun && !props.getProperty('LAST_SUCCESS_SECONDS')) {
      throw new Error('本取込み前にprepareGmailIngestLiveを実行してください');
    }
    const startedMs = Date.now();
    const successKey = dryRun ? 'LAST_DRY_RUN_SUCCESS_SECONDS' : 'LAST_SUCCESS_SECONDS';
    const cursorKey = 'INGEST_CURSOR';
    const nowSeconds = Math.floor(Date.now() / 1000);
    const saved = Number(props.getProperty(successKey));
    const cursor = JSON.parse(props.getProperty(cursorKey) || 'null');
    const resume = cursor && cursor.dryRun === dryRun ? cursor : null;
    const after = resume ? resume.after : Math.max(0, (saved || nowSeconds - 2 * 86400) - 2 * 3600);
    const before = resume ? resume.before : nowSeconds + 1;
    const query = `after:${after} before:${before} -in:drafts -in:chats`;
    let pageToken = resume ? resume.pageToken : undefined;
    let listed = 0;
    if (!dryRun) {
      const failed = JSON.parse(props.getProperty('FAILED_MESSAGE_IDS') || '[]');
      if (failed.length) {
        const retry = failed.slice(0, 10).map(gmailIngestMessage);
        const outcomes = gmailIngestPost(url, secret, repEmail, dryRun, retry, after, before);
        const remaining = failed.slice(10).concat(outcomes.filter(item => item.status === 'effect_failed').map(item => item.id));
        props.setProperty('FAILED_MESSAGE_IDS', JSON.stringify([...new Set(remaining)]));
      }
    }
    do {
      const options = {q: query, maxResults: 10};
      if (pageToken) options.pageToken = pageToken;
      let page;
      try {
        page = Gmail.Users.Messages.list('me', options);
      } catch (error) {
        // ページ位置が失効した場合も、次回は保存済み成功時刻から再走査できる。
        if (pageToken) props.deleteProperty(cursorKey);
        throw error;
      }
      const refs = page.messages || [];
      const messages = refs.map(ref => gmailIngestMessage(ref.id));
      const outcomes = gmailIngestPost(url, secret, repEmail, dryRun, messages, after, before);
      if (!dryRun) {
        const failed = JSON.parse(props.getProperty('FAILED_MESSAGE_IDS') || '[]');
        const next = [...new Set(failed.concat(outcomes.filter(item => item.status === 'effect_failed').map(item => item.id)))];
        if (next.length > 1000) throw new Error('後処理の失敗が1000件を超えました。ログを確認してください');
        props.setProperty('FAILED_MESSAGE_IDS', JSON.stringify(next));
      }
      listed += messages.length;
      pageToken = page.nextPageToken;
      if (pageToken) {
        props.setProperty(cursorKey, JSON.stringify({dryRun, after, before, pageToken}));
        if (Date.now() - startedMs > 4 * 60 * 1000) {
          console.log(JSON.stringify({listed, paused: true, dryRun}));
          return;
        }
      }
    } while (pageToken);
    // 全ページ成功時のみ進める。途中失敗は次回同じ範囲を再取得する。
    props.setProperty(successKey, String(before - 1));
    props.deleteProperty(cursorKey);
    console.log(JSON.stringify({listed, dryRun, completedAt: new Date().toISOString()}));
  } finally {
    lock.releaseLock();
  }
}

function gmailIngestMessage(id) {
  const raw = Gmail.Users.Messages.get('me', id, {
    format: 'metadata', metadataHeaders: ['From', 'To', 'Subject', 'Date']
  });
  const headers = (raw.payload && raw.payload.headers) || [];
  const header = name => {
    const found = headers.find(h => h.name.toLowerCase() === name.toLowerCase());
    return found ? found.value : '';
  };
  return {
    id: raw.id, thread_id: raw.threadId || null,
    from: header('From'), to: header('To'), subject: header('Subject'),
    date_header: header('Date'), snippet: raw.snippet || '',
    internal_date_ms: raw.internalDate || null
  };
}

function gmailIngestPost(url, secret, repEmail, dryRun, messages, after, before) {
  const response = UrlFetchApp.fetch(url, {
    method: 'post', contentType: 'application/json',
    headers: {'X-Webhook-Secret': secret}, muteHttpExceptions: true,
    payload: JSON.stringify({rep_email: repEmail, dry_run: dryRun, messages})
  });
  if (response.getResponseCode() !== 200) {
    throw new Error(`CRM取込み失敗 HTTP ${response.getResponseCode()}`);
  }
  const result = JSON.parse(response.getContentText());
  if (result.received !== messages.length || !Array.isArray(result.results)) {
    throw new Error('CRMの受付件数が一致しません');
  }
  console.log(JSON.stringify({
    after, before, dryRun, observedAt: new Date().toISOString(),
    results: result.results.map((item, index) => ({
      id: item.id, status: item.status, internal_date_ms: messages[index].internal_date_ms
    }))
  }));
  return result.results;
}

function prepareGmailIngestLive() {
  const props = PropertiesService.getScriptProperties();
  if (props.getProperty('LAST_SUCCESS_SECONDS')) throw new Error('本取込みは既に初期化されています');
  props.setProperty('LAST_SUCCESS_SECONDS', String(Math.floor(Date.now() / 1000)));
}

function installGmailIngestTrigger() {
  if (ScriptApp.getProjectTriggers().some(t => t.getHandlerFunction() === 'gmailIngest')) return;
  ScriptApp.newTrigger('gmailIngest').timeBased().everyMinutes(5).create();
}
