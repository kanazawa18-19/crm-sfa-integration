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
    const previous = JSON.parse(props.getProperty(cursorKey) || 'null');
    // 旧形式の途中位置は完走証拠が不足するので、成功位置から新しい走査としてやり直す。
    const rangeStart = Math.max(0, (saved || nowSeconds - 2 * 86400) - 2 * 3600);
    const cursor = previous && previous.dryRun === dryRun && previous.scanId ? previous : {
      scanId: Utilities.getUuid(), dryRun,
      after: rangeStart,
      before: Math.min(nowSeconds + 1, rangeStart + 31 * 86400), sequence: 0, listed: 0,
    };
    const {after, before} = cursor;
    const save = () => props.setProperty(cursorKey, JSON.stringify(cursor));
    const post = (messages, phase, sequence, listed, missingIds = []) => gmailIngestPost(
      url, secret, repEmail, dryRun, messages, after, before,
      {scan_id: cursor.scanId, phase, kind: 'natural', after, before, sequence, listed, missing_ids: missingIds}
    );
    const read = id => {
      try { return gmailIngestMessage(id); }
      catch (error) {
        // 確定消失だけを別記録へ移す。認証・一時失敗やHTTP応答不明では変更しない。
        if (!(error && (error.code === 404 || /(?:\b404\b|Requested entity was not found)/i.test(String(error.message || ''))))) throw error;
        // 永続証跡の受領後だけ進捗を破棄する。保存失敗なら同じIDで再通知する。
        post([], 'missing', cursor.sequence, cursor.listed, [id]);
        // 一覧の反映遅延で同じ消失IDが再出現しても走査全体を止め続けない。
        // この短期キャッシュを超えた過去IDもDB証跡には残る。
        const missing = JSON.parse(props.getProperty('MISSING_MESSAGE_IDS') || '[]');
        props.setProperty('MISSING_MESSAGE_IDS', JSON.stringify([...new Set(missing.concat(id))].slice(-1000)));
        const failed = JSON.parse(props.getProperty('FAILED_MESSAGE_IDS') || '[]');
        props.setProperty('FAILED_MESSAGE_IDS', JSON.stringify(failed.filter(value => value !== id)));
        // 元の走査は未完了のまま残す。同じ連番を別のID群で再送しない。
        props.deleteProperty(cursorKey);
        throw new Error('取得対象が消失しました。未完了走査を残して成功位置から再開します');
      }
    };
    save();
    // Gmail APIが最初のページで失敗しても、元の走査範囲を残す。
    post([], 'started', 0, 0);
    if (!dryRun && !cursor.retryDone) {
      const failed = JSON.parse(props.getProperty('FAILED_MESSAGE_IDS') || '[]');
      if (failed.length) {
        // 応答紛失時も同じID群を再送するため、試行対象をカーソルへ固定する。
        cursor.retryIds = cursor.retryIds || failed.slice(0, 10);
        save();
        const outcomes = post(cursor.retryIds.map(read), 'retry', 0, 0);
        const succeeded = outcomes.filter(item => item.status !== 'effect_failed').map(item => item.id);
        props.setProperty('FAILED_MESSAGE_IDS', JSON.stringify(failed.filter(id => !succeeded.includes(id))));
      }
      cursor.retryDone = true;
      save();
    }
    while (!cursor.terminalPending) {
      const options = {q: `after:${after} before:${before} -in:drafts -in:chats`, maxResults: 10};
      if (cursor.pageToken) options.pageToken = cursor.pageToken;
      let page;
      if (!cursor.pending) {
        try {
          page = Gmail.Users.Messages.list('me', options);
        } catch (error) {
          // 失効した継続トークンは破棄。旧走査は未完了としてDBに残す。
          if (cursor.pageToken) props.deleteProperty(cursorKey);
          throw error;
        }
      }
      // HTTP応答を失ってもGmailの一覧変動で同じ連番のID群が変わらないよう固定する。
      if (!cursor.pending) {
        const missing = JSON.parse(props.getProperty('MISSING_MESSAGE_IDS') || '[]');
        cursor.pending = {ids: (page.messages || []).map(ref => ref.id).filter(id => !missing.includes(id)), nextPageToken: page.nextPageToken || null};
        save();
      }
      const messages = cursor.pending.ids.map(read);
      const outcomes = post(messages, 'batch', cursor.sequence, 0);
      if (!dryRun) {
        const failed = JSON.parse(props.getProperty('FAILED_MESSAGE_IDS') || '[]');
        const next = [...new Set(failed.concat(outcomes.filter(item => item.status === 'effect_failed').map(item => item.id)))];
        if (next.length > 1000) throw new Error('後処理の失敗が1000件を超えました。ログを確認してください');
        props.setProperty('FAILED_MESSAGE_IDS', JSON.stringify(next));
      }
      cursor.listed += messages.length;
      cursor.sequence += 1;
      cursor.pageToken = cursor.pending.nextPageToken;
      cursor.terminalPending = !cursor.pageToken;
      delete cursor.pending;
      save();
      if (cursor.pageToken && Date.now() - startedMs > 4 * 60 * 1000) {
        console.log(JSON.stringify({listed: cursor.listed, paused: true, dryRun, after, before}));
        return;
      }
    }
    // 終端の永続保存が成功するまで成功位置を進めない。
    post([], 'complete', cursor.sequence, cursor.listed);
    props.setProperty(successKey, String(before - 1));
    props.deleteProperty(cursorKey);
    console.log(JSON.stringify({listed: cursor.listed, dryRun, after, before, scanComplete: true, completedAt: new Date().toISOString()}));
  } finally {
    lock.releaseLock();
  }
}

function gmailIngestMessage(id) {
  const full = PropertiesService.getScriptProperties().getProperty('EMAIL_REVIEW_ENABLED') === 'true';
  const raw = Gmail.Users.Messages.get('me', id, full ? {format: 'full'} : {
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
    internal_date_ms: raw.internalDate || null,
    ...(full ? {body_text: gmailReviewPlainText(raw.payload || {}), cc_header: header('Cc'), message_id_header: header('Message-ID')} : {})
  };
}

function gmailReviewPlainText(part) {
  if (part.filename || part.mimeType === 'message/rfc822') return '';
  if (part.mimeType === 'text/plain') {
    const encoded = (part.body || {}).data || '';
    if (encoded.length > 200000) return '';
    const contentType = ((part.headers || []).find(h => h.name.toLowerCase() === 'content-type') || {}).value || '';
    const match = contentType.match(/charset\s*=\s*["']?([\w-]+)/i);
    const charset = match ? match[1].toLowerCase() : 'utf-8';
    if (!['utf-8', 'us-ascii', 'iso-2022-jp', 'shift_jis', 'shift-jis', 'windows-31j', 'cp932'].includes(charset)) return '';
    try {
      const value = Utilities.newBlob(Utilities.base64DecodeWebSafe(encoded)).getDataAsString(charset);
      return value.length <= 20000 && !value.includes('\uFFFD') ? value : '';
    } catch (_) { return ''; }
  }
  if ((part.mimeType || '').startsWith('multipart/')) {
    const value = (part.parts || []).slice(0, 30).map(gmailReviewPlainText).filter(Boolean).join('\n');
    return value.length <= 20000 ? value : '';
  }
  return '';
}

function gmailIngestPost(url, secret, repEmail, dryRun, messages, after, before, observation) {
  const response = UrlFetchApp.fetch(url, {
    method: 'post', contentType: 'application/json',
    headers: {'X-Webhook-Secret': secret}, muteHttpExceptions: true,
    payload: JSON.stringify({rep_email: repEmail, dry_run: dryRun, messages, observation})
  });
  if (response.getResponseCode() !== 200) {
    // メールの中身を出さず、入力制限に抵触した項目を切り分ける。
    const maxLengths = {};
    messages.forEach(message => Object.keys(message).forEach(key => {
      if (typeof message[key] === 'string') maxLengths[key] = Math.max(maxLengths[key] || 0, message[key].length);
    }));
    console.log(JSON.stringify({httpStatus: response.getResponseCode(), maxLengths}));
    throw new Error(`CRM取込み失敗 HTTP ${response.getResponseCode()}`);
  }
  const result = JSON.parse(response.getContentText());
  if (result.received !== messages.length || !Array.isArray(result.results) ||
      result.results.length !== messages.length || result.results.some((item, i) => item.id !== messages[i].id) ||
      result.observation_saved !== true) {
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
