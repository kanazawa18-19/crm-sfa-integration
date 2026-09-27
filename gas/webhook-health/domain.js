/* 観測の分類と通知計画。GASやネットワークに依存しない。 */
var WebhookHealth = (function () {
  'use strict';
  var HOUR = 3600000;
  // 毎時トリガーの時刻の揺れを許容し、手動の連打では連続確認にならないようにする。
  var RECHECK_DELAY = HOUR * 0.8;
  var KEYS = ['receipt_store', 'notion_delivery', 'kintone_delivery', 'zoho_subscription', 'zoho_delivery', 'spreadsheet_delivery'];
  var LABELS = {receipt_store: '受信記録', notion_delivery: 'Notion受信', kintone_delivery: 'kintone受信', zoho_subscription: 'Zoho購読', zoho_delivery: 'Zoho受信', spreadsheet_delivery: 'シート受信', monitor: '診断API'};
  var STATUSES = ['ok', 'unverified', 'pending', 'warning', 'critical', 'unknown'];
  function validate(report, now) {
    if (!report || report.schema_version !== 1 || !Array.isArray(report.checks) || report.checks.length !== KEYS.length) throw new Error('診断応答が不正です');
    var age = now - Date.parse(report.observed_at);
    if (!Number.isFinite(age) || age < -60000 || age > 15 * 60000) throw new Error('診断応答の時刻が不正です');
    var seen = {};
    report.checks.forEach(function (c) {
      if (!c || KEYS.indexOf(c.key) < 0 || seen[c.key] || STATUSES.indexOf(c.status) < 0 || typeof c.reason !== 'string' || c.reason.length > 150) throw new Error('診断項目が不正です');
      seen[c.key] = true;
      if (c.status === 'pending' && (!Number.isFinite(Date.parse(c.activity_at)) || Date.parse(c.activity_at) > now + 60000)) throw new Error('変更時刻が不正です');
      ['last_received_at', 'expires_at'].forEach(function (key) {
        if (c[key] != null && !Number.isFinite(Date.parse(c[key]))) throw new Error('観測日時が不正です');
      });
    });
    return report;
  }
  function dateJst(ms) { return new Date(ms + 9 * HOUR).toISOString().slice(0, 10); }
  function plan(checks, previous, now) {
    var next = JSON.parse(JSON.stringify(previous || {checks: {}}));
    next.checks = next.checks || {};
    var events = [];
    var lines = [];
    checks.forEach(function (c) {
      var old = next.checks[c.key] || {};
      var same = old.raw === c.status && (c.status !== 'pending' || old.receipt === c.last_received_at);
      // 実変更が続いても最初の観測時刻を維持し、猶予を延ばさない。
      var first = same && Number.isFinite(old.first) ? old.first : now;
      var activity = same && Number.isFinite(old.activity) ? old.activity : Date.parse(c.activity_at);
      var level = c.status;
      var reason = c.reason;
      if (level === 'pending') {
        if (now - first >= RECHECK_DELAY && now - Math.min(activity, first) >= 2 * HOUR) {
          level = 'warning';
          reason = c.key === 'kintone_delivery' ? '変更後の受信なし（CSV・一括操作では通知されないため要確認）' : '変更から2時間以上、再確認でも受信が進んでいません';
        } else reason = '変更後の受信待ち（確認猶予内）';
      }
      if (level === 'unknown' && now - first < RECHECK_DELAY) level = 'pending';
      var state = {raw: c.status, first: first, activity: Number.isFinite(activity) ? activity : null, receipt: c.last_received_at || null,
        notified: old.notified || null, notifiedAt: old.notifiedAt || null};
      // 変更が続いていても受信が進めば、前の停滞は解消したと区別する。
      if (c.status === 'pending' && old.notified && c.last_received_at &&
          (!old.receipt || Date.parse(c.last_received_at) > Date.parse(old.receipt))) {
        events.push({key: c.key, level: 'recovered', text: LABELS[c.key] + '：受信が再開しました（次の変更は確認猶予内）'});
        state.notified = null; state.notifiedAt = null;
      }
      var abnormal = ['warning', 'critical', 'unknown'].indexOf(level) >= 0;
      if (abnormal && (state.notified !== level || !state.notifiedAt || now - state.notifiedAt >= 24 * HOUR)) {
        events.push({key: c.key, level: level, text: LABELS[c.key] + '：' + reason});
        state.notified = level; state.notifiedAt = now;
      } else if (level === 'ok' && state.notified) {
        events.push({key: c.key, level: 'recovered', text: LABELS[c.key] + '：確認できる状態に戻りました'});
        state.notified = null; state.notifiedAt = null;
      }
      // unknown/unverifiedは復旧扱いにしない。
      next.checks[c.key] = state;
      lines.push(LABELS[c.key] + '：' + reason + (c.last_received_at ? '（最終受信 ' + new Date(Date.parse(c.last_received_at) + 9 * HOUR).toISOString().slice(0, 16).replace('T', ' ') + ' JST）' : ''));
    });
    var daily = next.daily !== dateJst(now);
    if (daily) next.daily = dateJst(now);
    return {next: next, events: events, daily: daily, lines: lines};
  }
  return {validate: validate, plan: plan};
}());
if (typeof module !== 'undefined') module.exports = WebhookHealth;
