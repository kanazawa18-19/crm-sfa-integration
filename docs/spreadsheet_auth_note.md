# Google スプレッドシート認証に関する申し送り（Phase 2 実装ノート）

`src/sync_engine/clients/spreadsheet_client.py` の `HttpSpreadsheetClient` は、
Google Sheets API v4 への認証を簡略化した方式で実装している。本ノートはその内容と、
本番運用に向けて必要な置き換え作業を申し送るものである。

## 現状の実装（簡略認証）

サービスアカウントのJWT署名によるアクセストークン取得は実装が複雑なため、本実装では
呼び出し元が有効なOAuth2アクセストークンを`GOOGLE_ACCESS_TOKEN`環境変数
（または明示的な`access_token`引数）で直接用意している前提の、Bearerトークン認証のみを
実装している。

- コンストラクタで`GOOGLE_ACCESS_TOKEN`（または`access_token`引数）・`SPREADSHEET_ID`
  （または`spreadsheet_id`引数）のいずれかが未設定の場合、原因不明な401を避けるため
  `ValueError`で即座に失敗する。
- トークンの自動リフレッシュは行わない。有効期限が切れた場合、`GOOGLE_ACCESS_TOKEN`を
  手動で再取得・再設定するまで全リクエストが401で失敗し続ける。

## 本番運用に必要な対応

本番投入前に、サービスアカウントのJWTからアクセストークンを取得・キャッシュし、
有効期限が近づいたら自動的にリフレッシュする処理への置き換えが必要。実装イメージは
`HttpZohoClient`（`src/sync_engine/clients/zoho_client.py`）のOAuth2トークンキャッシュ・
リフレッシュ処理（`_get_access_token`/`_refresh_access_token`、`threading.Lock`による
並行アクセス時の排他制御を含む）を参考にできる。

必要な作業:

1. `GOOGLE_SERVICE_ACCOUNT_JSON`（`config/.env.example`に既に項目あり）からサービス
   アカウントの秘密鍵を読み込み、JWTを組み立てて署名する処理を追加する。
2. Google OAuth2トークンエンドポイント（`https://oauth2.googleapis.com/token`）へJWTを
   渡してアクセストークンを取得する処理を追加する。
3. 取得したアクセストークンをメモリ内でキャッシュし、有効期限が切れるまで再利用する
   （Zohoクライアントと同様の設計）。
4. `HttpSpreadsheetClient`の`access_token`引数・`GOOGLE_ACCESS_TOKEN`環境変数への
   依存を上記の自動取得処理へ差し替える。

## トークン有効期限切れ時の暫定運用手順（本番切替までの間）

サービスアカウントJWTからの自動取得に置き換えるまでの間、`GOOGLE_ACCESS_TOKEN`は
Google発行のOAuth2アクセストークン（有効期限は通常1時間程度）を手動で払い出し、
環境変数として設定する運用となる。有効期限切れによる同期停止を避けるため、以下の
暫定手順を踏むこと。

1. Google Cloud Console またはOAuth 2.0 Playground等で、対象スプレッドシートへの
   アクセス権を持つアカウント（サービスアカウント推奨）のアクセストークンを再発行する。
2. 再発行したトークンを`GOOGLE_ACCESS_TOKEN`環境変数へ設定し、同期エンジンの実行環境
   （Lambda/Cloud Functions等）を再デプロイまたは環境変数を更新する。
3. 有効期限切れの間に発生した同期失敗（401エラー）は、失敗イベントのリトライ・再送
   の仕組み（未実装の場合は手動での再実行）で解消する。
4. 恒常的な運用では有効期限切れが頻発するため、上記「本番運用に必要な対応」を優先度
   高く実施すること。
