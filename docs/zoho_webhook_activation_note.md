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

6. 有効期限切れ前の定期的な延長は、以下の「自動延長（Vercel Cron）」節の通り
   `GET /api/cron/zoho-webhook-renewal`が6時間毎に自動で行う。**新規登録直後（手順4）は
   必ずVercel本番環境変数`ZOHO_WATCH_CHANNEL_ID`へも同じchannel_idを設定すること**
   （自動延長がこの値を参照するため。詳細は下記節を参照）。

---

## 自動延長（Vercel Cron）

Zohoのwatchチャンネルは登録・延長時点から**最大1日**で失効し、放置すると
`/api/webhooks/zoho`への通知が無音で止まる（エラーが表面化しない）。これを防ぐため、
`GET /api/cron/zoho-webhook-renewal`（`src/api/app.py`）をVercel Cronから**6時間毎**
（`vercel.json`の`crons`、`0 */6 * * *`）に自動起動し、`PUT /crm/v3/actions/watch`で
延長し続ける。1日の猶予に対して6時間毎という余裕を持たせているのは、1回の失敗・
遅延だけではチャンネルが実際に失効しないようにするため。

### 仕組み

- 認証は`/api/cron/daily-batch`と全く同じパターン（`src/api/auth.py`の
  `verify_cron_secret`、`Authorization: Bearer $CRON_SECRET`のfail-closed検証、
  VercelがCron Job実行時に自動付与する）。
- 実際の延長ロジックは`src/sync_engine/zoho_watch_channel.py`の
  `renew_zoho_watch_channel()`に切り出してあり、`scripts/register_zoho_webhook.py`
  （手動CLI）とpayload組み立て・API呼び出し（`build_watch_payload`/
  `register_or_renew_watch`）を共有する（重複実装を避けるための共通化。
  本タスクで`scripts/register_zoho_webhook.py`側の同名関数はこのモジュールへ移設し、
  スクリプト側はインポートして使うだけになっている）。
- `run_zoho_webhook_renewal()`（`src/api/app.py`）は`renew_zoho_watch_channel()`を
  呼び、結果をJSONで返す（`{"status": "success", "channel_id": ..., "channel_expiry": ...}`）。

### channel_idの取得元（設計判断）

`.zoho_watch_channel.json`（`scripts/register_zoho_webhook.py --yes`実行時に
channel_id/channel_expiryを保存するローカルファイル、リポジトリ直下、`.gitignore`対象）は、
手動CLIをローカルシェルで実行した際の**ローカルファイルシステム上にしか存在しない**。
Vercelのサーバーレス関数（`/api/cron/zoho-webhook-renewal`）はデプロイのたびに作り直される
別のファイルシステム上で動作するため、このファイルへは一切アクセスできない
（`NotionIdMappingStore`がIDマッピングの永続化にNotionページを使っているのと同種の制約。
このセッションで既に対応済みの問題と同じクラス）。

このため、自動延長は`.zoho_watch_channel.json`に頼らず、**Vercel本番環境変数
`ZOHO_WATCH_CHANNEL_ID`をchannel_idの一次情報源とする**（`renew_zoho_watch_channel()`が
`channel_id`引数省略時にこの環境変数を読む）。同様にnotify_url組み立て用のデプロイ
ベースURLも環境変数`ZOHO_WEBHOOK_BASE_URL`（例:
`https://crm-sfa-integration.vercel.app`）から取得する。

代替案として「Zoho側に登録済みチャンネルの一覧を問い合わせるGET APIを使い、
channel_idを一切保存せずに毎回発見する」方式も検討したが、Zoho CRM Notifications API v3の
公式ドキュメントには、既知のchannel_idを指定せずに一覧取得できるGETエンドポイントの
記載が見当たらず（`GET /crm/v3/actions/watch`は特定channel_idの詳細取得用と見られる）、
本タスクは実際の本番Zoho APIへ新たに到達して仕様を確認すること自体がスコープ外だった
ため、未検証のままこの方式を採用することは避けた。環境変数方式は追加のAPI呼び出しが
不要な上、`scripts/register_zoho_webhook.py`が新規登録成功時に既に出力している
`ZOHO_WATCH_CHANNEL_ID=... ZOHO_WATCH_EXPIRY=...`という1行をそのままVercel環境変数へ
反映するだけでよく、既存の運用フローと自然に噛み合う。

**運用上必ず守ること**: `scripts/register_zoho_webhook.py --yes`で**新規登録**（`--channel-id`
指定なし・`.zoho_watch_channel.json`も存在しない状態での実行）を行うたびに、出力される
`ZOHO_WATCH_CHANNEL_ID=...`の値を、Vercel本番環境変数`ZOHO_WATCH_CHANNEL_ID`へも
手動で反映すること（`vercel env add ZOHO_WATCH_CHANNEL_ID`。本タスクでは自動化していない
＝実行していない。反映は別途ユーザー側で確認・実施する）。新規登録は通常一度きりの
はずで、それ以降のcronによる延長ではchannel_id自体は変わらない（PUTは同じchannel_idを
指定し続けるだけ）ため、一度反映すれば継続的な手動作業は不要になる。

### 延長に失敗した場合

- `ZOHO_WATCH_CHANNEL_ID`が未設定（一度も上記の反映を行っていない等）の場合、
  `run_zoho_webhook_renewal()`はZoho APIへは到達せず、HTTP 500
  （`ZohoWatchChannelNotConfiguredError`、レスポンスボディの`detail`に原因を明記）を返す。
  「成功したように見えるno-op」にはならない。
- Zoho API呼び出し自体が失敗した場合（トークン失効・INVALID_DATA等）はHTTP 502
  （`ZohoApiError`）を返す。
- いずれの場合もVercelのCron Job実行結果は非2xxレスポンスとして失敗扱いになり、
  Vercelダッシュボードの当該プロジェクト → Cron Jobs（または Deployments → Functions
  のログ）で実行履歴・エラー内容を確認できる。
- 復旧手順: まず`ZOHO_WATCH_CHANNEL_ID`/`ZOHO_WEBHOOK_BASE_URL`/`ZOHO_WEBHOOK_SECRET`等の
  Vercel環境変数が正しく設定されているか確認する。channel_id自体が失効・削除されて
  しまっている疑いがある場合は、ローカルから`scripts/register_zoho_webhook.py --yes`を
  （必要なら`--channel-id`を明示して、または省略して新規登録として）手動実行し、
  新しいchannel_idが発行された場合は上記「運用上必ず守ること」に従って
  `ZOHO_WATCH_CHANNEL_ID`を更新する。

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
