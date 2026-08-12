# Zoho CRM Webhook 有効化手順

## 手順

1. Webhookシークレットを発行する（未発行の場合のみ）。

   ```
   python scripts/generate_webhook_secret.py
   ```

2. 発行した値を **Vercel本番環境変数** `ZOHO_WEBHOOK_SECRET` に設定する。

3. **ローカルシェル** でも同じ値を `ZOHO_WEBHOOK_SECRET` としてexportする（`--token` で
   明示指定してもよい）。

   重要: Vercel本番環境変数のZOHO_WEBHOOK_SECRETと、これから実行するローカルシェルの
   ZOHO_WEBHOOK_SECRETは**別物**であり、両方を同じ値に揃えないと意味がない。手順2だけ
   実施してこの手順3を忘れると、`register_zoho_webhook.py --yes` はローカルの空token
   （または食い違うtoken）でZoho側の購読登録を「成功」させてしまい、受信側
   （`zoho_webhook.py`）は以後すべての通知を401で拒否し続ける「登録済みだが機能しない」
   状態に陥る。これを防ぐため、`register_zoho_webhook.py`は`--yes`指定時にtokenが空だと
   `--allow-empty-token`を明示しない限り登録自体を拒否する（詳細は後述の設計節を参照）。

   ```
   export ZOHO_WEBHOOK_SECRET=<Vercel本番に設定した値と同じもの>
   ```

4. 実際にZoho CRM側へ購読（watch）を登録する。ユーザーの明示的な確認を得てから実行すること。

   ```
   python scripts/register_zoho_webhook.py \
       --base-url https://crm-sfa-integration.vercel.app --yes
   ```

   成功すると、標準出力に `ZOHO_WATCH_CHANNEL_ID=... ZOHO_WATCH_EXPIRY=...` の1行と、
   `.zoho_watch_channel.json`（リポジトリ直下、`.gitignore`対象）への保存が行われる。
   次回、有効期限切れ前に延長更新する際は、`--channel-id`を省略すればこのファイルの
   channel_idが自動的に延長対象として使われる（明示的に別チャンネルを指定したい場合は
   `--channel-id`で上書きする）。

5. 有効化後の動作確認チェックリスト。

   1. Zoho CRMで対象モジュール（`Deals`）のレコードを1件編集する。
   2. Vercelのfunction logsで `/api/webhooks/zoho` への200レスポンスを確認する。
   3. 対応するNotionページが更新されたか確認する。

6. （本タスクのスコープ外・別フォローアップ）有効期限切れ前の定期的な再登録
   （`--channel-id`指定なしでの延長）をcron等へ配線する。

---

## 背景・設計メモ（prep-onlyタスクの記録）

`src/sync_engine/webhook_handlers/zoho_webhook.py` は Zoho CRM Notification Webhook の
**受信ハンドラ**として既に実装済み（`POST /api/webhooks/zoho`、`src/api/app.py`）。
着信リクエストの認証には、`src/sync_engine/webhook_handlers/_common.py` の
`verify_webhook_body_token(payload, token_field="token", env_var="ZOHO_WEBHOOK_SECRET")`
（後述の「解決済み」節を参照。通知ペイロードbody内の`token`フィールド方式、fail-closed）を
使っている。

しかし本タスク着手時点で、以下が未整備だった。

- `ZOHO_WEBHOOK_SECRET` が `config/.env` にもVercel本番環境変数にも未設定（一度も発行されていない）。
- Zoho CRM側にNotification（watch）購読を登録するスクリプト・手順がリポジトリ内に存在しない。
  Zohoのwatchチャンネルには有効期限があり、期限切れで放置すると通知が無音で止まる。

### 本タスクで用意したもの

1. `scripts/generate_webhook_secret.py` — `secrets.token_urlsafe(32)` を標準出力するだけの
   ユーティリティ。既存の`*_WEBHOOK_SECRET`/`*_API_TOKEN`をどう生成したかのprecedentが
   リポジトリ内に見当たらなかったため、他のトークンと同様のURL-safeなランダム値として実装した。
   `config/.env`書き込みや外部呼び出しは一切行わない（値は手動でVercel環境変数等へ設定する）。
2. `scripts/register_zoho_webhook.py` — Zoho CRM Notifications（watch）APIへ
   `Deals`モジュール（`PROJECT_SCHEMA.zoho_api_module`）の購読を新規登録／更新（延長）する
   スクリプト。既存 `HttpZohoClient`（`src/sync_engine/clients/zoho_client.py`）のトークン
   リフレッシュ・キャッシュ・リトライをそのまま再利用するよう、`HttpZohoClient.request()`
   （任意の絶対URLへ認証ヘッダー付きで送る汎用メソッド）を追加して実装した。
   常にdry-run表示（何を送るか。tokenは`***REDACTED***`に伏せて表示する）を先に出力し、
   `--yes`を明示指定した場合のみ実際にAPIへ送る（本タスク中は一度も`--yes`付きで実行して
   いない＝実際の購読登録は行っていない）。`--yes`指定時にtokenが空の場合は
   `--allow-empty-token`を明示しない限り拒否する（BLOCKER3対策）。登録成功時は
   channel_id/channel_expiryを`.zoho_watch_channel.json`へ保存し、次回実行時に
   `--channel-id`省略時のデフォルト（延長対象）として読み戻す（WARN4対策）。
3. `tests/scripts/test_register_zoho_webhook.py` / `tests/scripts/test_generate_webhook_secret.py`
   — `requests_mock`でZoho APIをモックし、実ネットワーク呼び出し無しでペイロード構築・
   新規登録(POST)/更新(PUT)の分岐・エラー処理・token非露出・空token拒否・状態永続化を検証。

### 解決済み: events配列の形式とchannel_expiryの上限（本番Zohoへの実送信で発覚・修正済み）

`--yes`を付けた実送信（dry-run+`--yes`）を本番Zohoへ試したところ、初回実装の`events`配列
（`[{"channel_id": ..., "module": ...}]`というオブジェクト配列）が
`HTTP 202: {'code': 'INVALID_DATA', 'details': {'api_name': 'events', 'json_path': '$.watch[0].events'}}`
で拒否された。Zoho公式ドキュメント記載のリクエストスキーマを確認し、以下のように修正済み。

- `events`は`"{モジュールAPI名}.{create|delete|edit|all}"`形式の文字列を並べたフラットな
  配列。本スクリプトは対象モジュール全体を監視したいため`["{module}.all"]`（既定なら
  `["Deals.all"]`）を送る。
- `channel_expiry`はZoho側の制約により登録・延長時点から**最大1日先まで**。それを超える
  `--expiry-days`を指定すると、実際にAPIへ送る前に明確なエラーで拒否する
  （`register_zoho_webhook.py`の`validate_expiry_days()`）。既定値も`7`日から`1`日に変更済み。

### 解決済み: Zoho通知はHTTPヘッダーでの認証をサポートしないため、body内`token`方式で検証する

Zoho CRM Notifications（watch）APIの登録リクエスト（`POST/PUT /crm/v3/actions/watch`）には、
Zohoが送信する通知に任意のHTTPヘッダーを付与させる仕組みが無い。代わりに登録時に
`token`という文字列フィールドを指定でき、Zohoは通知を送る際にこの値を
**通知ペイロードのJSON body内の`token`キー**としてそのまま返してくる（HTTPヘッダーではない）。

この理解はZoho CRM API v3 Notifications公式ドキュメントの記載内容に基づくものだが、本タスクは
実際に本番APIへ到達させて挙動を確認すること自体が禁止のスコープだったため**未検証**。
実際に有効化する前に、Zoho公式ドキュメント
（https://www.zoho.com/crm/developer/docs/api/v3/notifications.html）で最新仕様を再確認すること。

このため、他ハンドラ共通の `verify_webhook_secret()`（`X-Webhook-Secret` **ヘッダー**を見る実装）
はZohoには使えず、代わりに `_common.py` に `verify_webhook_body_token(body, token_field, env_var)`
を新設し、`zoho_webhook.py`の`handler()`はこちらを使うよう改修済み（別タスクで対応）。
`handler()`は、bodyが構文的に正しいJSONでも辞書でない場合（`null`/配列/数値/文字列/真偽値）は
`verify_webhook_body_token()`を呼ぶ前に400を返す（未認証の送信者でも到達できる経路のため、
認証チェック自体の中でcrashしないようにするBLOCKER2対策）。

#### 実際の認証の流れ

1. `scripts/register_zoho_webhook.py` が `--token`（既定値は環境変数 `ZOHO_WEBHOOK_SECRET`）を
   `watch` 登録リクエストの `token` フィールドとしてZohoへ送る。
2. Zohoは以後の通知POST（`notify_url`宛て）のJSON body内に、登録時と同じ値を
   `{"token": "...", "module": "...", ...}` の形で毎回そのまま含めて送ってくる。
3. `zoho_webhook.py`の`handler()`は着信bodyをJSONパースした後、
   `verify_webhook_body_token(payload, token_field="token", env_var="ZOHO_WEBHOOK_SECRET")`
   を呼び、`payload["token"]`を環境変数`ZOHO_WEBHOOK_SECRET`と`hmac.compare_digest`で
   （タイミングセーフに）比較する。一致しなければ401（`unauthorized_response()`）を返す。
4. `ZOHO_WEBHOOK_SECRET`が未設定の場合は、`ALLOW_UNSIGNED_WEBHOOKS=true`を明示しない限り
   fail-closed（常に401）。他ハンドラの`verify_webhook_secret()`と同じ姿勢。
5. 登録スクリプト（1.）と受信ハンドラ（3.）は同じ`ZOHO_WEBHOOK_SECRET`環境変数を参照するため、
   Vercel本番環境変数へ一度設定すれば両者は自動的に一致する **はずだが**、これはあくまで
   「Vercel本番のZOHO_WEBHOOK_SECRET」と「登録スクリプトを実行するローカルシェルの
   ZOHO_WEBHOOK_SECRET」を運用者自身が同じ値に揃えた場合の話であり、自動では揃わない
   （上記「手順」節の手順3を参照）。
