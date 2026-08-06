"use strict";

/**
 * GAS 編集トリガー（05_同期・競合制御「変更検知の仕組み」：スプレッドシート = GASのonEdit）。
 *
 * 編集された行の値をヘッダー名キーのレコードへ整形し、同期エンジンのWebhook
 * エンドポイント（src/sync_engine/webhook_handlers/spreadsheet_webhook.py）へPOSTする。
 *
 * ■ なぜ関数名を `onEdit` にしていないか（インストーラブルトリガーとして登録する）
 * GASにはファイル内に `onEdit(e)` という名前の関数があると自動的に実行される「単純トリガー」
 * の仕組みがあるが、単純トリガーは権限が制限されており、UrlFetchApp（外部への
 * HTTPリクエスト）を呼べない。Webhook送信には完全な認可を持つ「インストーラブルトリガー」
 * としての登録が必須なため、この関数はあえて `onEditSync` と名付け、setupTemplate.js の
 * installEditTrigger_() で `ScriptApp.newTrigger('onEditSync').forSpreadsheet(ss).onEdit().create()`
 * により明示的に登録する運用とする。
 *
 * ■ 手動テスト手順（GASにはJest/pytest相当の実行環境が無いため）
 * 1. Apps Scriptエディタで本プロジェクトを開き、setupTemplate.js の setupAll() を一度実行し、
 *    タブ作成とインストーラブルトリガーの登録を行う（初回のみ、権限承認が必要）。
 * 2. 「プロジェクトの設定」＞「スクリプト プロパティ」に SPREADSHEET_WEBHOOK_URL と
 *    SPREADSHEET_WEBHOOK_SECRET を設定する（SPREADSHEET_WEBHOOK_SECRET の値は
 *    config/.env の同名変数と揃えること）。
 * 3. 業務タブ（例:「案件管理」）の2行目以降のセルを実際に編集する。
 * 4. Apps Scriptエディタの「実行数」ログで onEditSync が呼び出され、postToWebhook_ 内の
 *    UrlFetchApp.fetch のレスポンスステータスが200であることを確認する
 *    （同期エンジン側を ALLOW_UNSIGNED_WEBHOOKS=true のローカル開発サーバーとして立て、
 *    ngrok等でURLを一時公開して疎通確認する運用を想定）。
 * 5. ペイロード整形・タブ判定・ループガード判定などの純粋ロジック部分は payloadUtils.js /
 *    tabDefinitions.js に切り出し済みで、`node --test gas/payloadUtils.test.js` で検証できる。
 *
 * ■ Webhook送信失敗に運用者が気づく方法（postToWebhook_はconsole.errorを出すのみで、
 *    スプレッドシート上には何もエラー表示をしない）
 * Apps Scriptエディタ左メニューの「実行数」（Executions）を開くと、onEditSync の実行ごとの
 * ログが確認できる。console.error で出力した "spreadsheet webhook failed: ..." 等の文言で
 * 検索すれば、どの編集がWebhook送信に失敗したかを追える。「トリガー」画面から失敗時の
 * 通知メール送信を設定することもできる（Apps Scriptエディタ左メニュー「トリガー」＞
 * 対象トリガーの編集＞「エラー通知設定」）。
 *
 * tabDefinitions.js / payloadUtils.js のグローバル変数・関数（BUSINESS_SHEET_NAMES,
 * isSyncManagedSheet 等）は、GASが同一プロジェクト内の全ファイルを1つのグローバル
 * スコープへ連結して実行するため、import/require無しでそのまま参照できる。
 */

var SYNC_WRITE_GUARD_PROPERTY = "SYNC_WRITE_IN_PROGRESS_AT";
var SYNC_WRITE_GUARD_TTL_MS = 5000; // GAS側書き込み経路が将来追加された場合の保険的ウィンドウ

function onEditSync(e) {
  var range = e.range;
  var sheetName = range.getSheet().getName();

  if (!isSyncManagedSheet(sheetName, BUSINESS_SHEET_NAMES)) return;
  if (isSyncWriteInProgress_()) return; // 二次防御（詳細はファイル冒頭コメント参照）

  // 複数行にまたがる貼り付け・オートフィルでもonEditは1回しか発火せず、e.rangeが
  // 編集範囲全体（複数行）を表す。開始行だけを見ると2行目以降が静かに同期漏れするため、
  // 範囲内の全データ行（ヘッダー行を除く）を個別に処理する。
  var rows = dataRowNumbersInRange(range.getRow(), range.getNumRows());
  if (rows.length === 0) return;

  var sheet = range.getSheet();
  var ss = sheet.getParent();
  var timeZone = ss.getSpreadsheetTimeZone();
  var lastColumn = sheet.getLastColumn();
  var headerRow = sheet.getRange(1, 1, 1, lastColumn).getValues()[0];
  var editedAt = new Date().toISOString();

  rows.forEach(function (row) {
    var rowValues = sheet.getRange(row, 1, 1, lastColumn).getValues()[0];
    var values = rowValuesToRecord(headerRow, rowValues, {
      // 削除フラグは delete_record 専用の仕組みであり、通常編集の同期対象プロパティ
      // としてWebhookへ送るとPython側のDBスキーマに存在せずKeyErrorになるため除外する。
      excludedHeaders: [DELETE_FLAG_COLUMN],
      // 日付セルをJSON化する際のUTC変換によるオフバイワン日ズレを防ぐため、
      // スプレッドシートのタイムゾーンを基準にした日付文字列に明示的に変換する。
      formatDate: function (date) {
        return Utilities.formatDate(date, timeZone, "yyyy-MM-dd");
      },
    });
    var payload = buildEditPayload(sheetName, row, editedAt, values);
    postToWebhook_(payload);
  });
}

function postToWebhook_(payload) {
  var props = PropertiesService.getScriptProperties();
  var url = props.getProperty("SPREADSHEET_WEBHOOK_URL");
  var secret = props.getProperty("SPREADSHEET_WEBHOOK_SECRET");
  if (!url || !secret) {
    console.error(
      "SPREADSHEET_WEBHOOK_URL/SPREADSHEET_WEBHOOK_SECRET が未設定のためWebhook送信をスキップしました"
    );
    return;
  }

  var response = UrlFetchApp.fetch(url, {
    method: "post",
    contentType: "application/json",
    headers: { "X-Webhook-Secret": secret },
    payload: JSON.stringify(payload),
    muteHttpExceptions: true,
  });

  if (response.getResponseCode() >= 300) {
    console.error(
      "spreadsheet webhook failed: " + response.getResponseCode() + " " + response.getContentText()
    );
  }
}

function isSyncWriteInProgress_() {
  var markedAt = PropertiesService.getScriptProperties().getProperty(SYNC_WRITE_GUARD_PROPERTY);
  return isWithinSyncWriteWindow(Date.now(), markedAt ? Number(markedAt) : null, SYNC_WRITE_GUARD_TTL_MS);
}

/**
 * 将来、同期エンジンがGAS Web App（doPost）経由でSpreadsheetAppへ書き込む構成に変わった
 * 場合、その書き込み処理の直前にこの関数を呼ぶこと（現状はどこからも呼ばれていない）。
 */
function markSyncWriteStart_() {
  PropertiesService.getScriptProperties().setProperty(SYNC_WRITE_GUARD_PROPERTY, String(Date.now()));
}
