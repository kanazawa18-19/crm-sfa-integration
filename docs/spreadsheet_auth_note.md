# Google スプレッドシート認証に関する申し送り（Phase 2 実装ノート）

`src/sync_engine/clients/spreadsheet_client.py` の `HttpSpreadsheetClient` の
Google Sheets API v4 向け認証について、現行の実装内容を申し送るものである。

## 現状の実装（サービスアカウント自動リフレッシュ）

アクセストークンの解決は`src/document_generation/google_auth.py`の
`get_google_access_token()`に委譲している。サービスアカウント
（`GOOGLE_SERVICE_ACCOUNT_JSON`環境変数、JSON文字列そのもの）を最優先で使い、
JWTの組み立て・署名・トークン取得・キャッシュ・有効期限が近づいた際の自動リフレッシュを
`google-auth`ライブラリ（`google.oauth2.service_account.Credentials`）が行う
（複数スレッドからの同時アクセスに対しては`threading.Lock`による排他制御あり）。
`HttpSpreadsheetClient`はリクエストの都度`get_google_access_token()`を呼び出し、
常駐プロセスで使い回されても失効したトークンを使い続けることはない。

- コンストラクタで`SPREADSHEET_ID`（または`spreadsheet_id`引数）が未設定の場合、
  `ValueError`で即座に失敗する。
- `access_token`引数を明示指定しなかった場合、構築時に一度だけ
  `get_google_access_token()`を呼び、有効な認証情報（サービスアカウントJSONまたは
  手動トークン）が何かしら解決できることを検証する（fail-fast）。この時点で
  `GOOGLE_SERVICE_ACCOUNT_JSON`・`GOOGLE_ACCESS_TOKEN`のいずれも未設定であれば
  `ValueError`を送出する。この検証結果（トークン値そのもの）はキャッシュせず、
  以降のリクエストは`_headers()`で毎回`get_google_access_token()`を再解決する
  （構築時の一度きりのチェックは、あくまで「認証情報が丸ごと欠落している」場合に
  実際のディスパッチ時ではなく起動時に検知するための健全性チェック）。
- `production_wiring.build_spreadsheet_targets_by_db()`は上記`ValueError`を
  catchし、スプレッドシート向け同期を無効化（空辞書を返す）した上でログに警告を出す。

## ローカル開発向けの上書き

ローカル動作確認等でサービスアカウントを用意しない場合は、`GOOGLE_ACCESS_TOKEN`
環境変数（または`HttpSpreadsheetClient`への明示的な`access_token`引数）に
有効なOAuth2アクセストークン（通常1時間程度で失効）を直接設定することで代替できる
（`get_google_access_token()`はサービスアカウントが未設定の場合のフォールバックとして
これを参照する）。本番運用ではサービスアカウント自動リフレッシュを前提とし、
`GOOGLE_ACCESS_TOKEN`は使用しない。
