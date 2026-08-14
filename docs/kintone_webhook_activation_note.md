# kintone Webhook（kintone→Notion方向）有効化手順

## 前提: kintoneのWebhook機能はカスタムHTTPヘッダーを送信できない

他ハンドラの多くが使う`verify_webhook_secret()`（`X-Webhook-Secret`ヘッダー方式）は
kintoneには使えない。kintoneのアプリ単位Webhook設定画面（2026-08-14、kintone公式ヘルプ
`jp.kintone.help/k/ja/app/set_webhook/webhook.html`で確認済み）で設定できる項目は以下のみ:

- 説明（64文字まで）
- Webhook URL（1,024文字まで）
- 通知を送信する条件（レコードの追加/編集/削除、コメントの書き込み、ステータスの更新）
- このWebhookを有効にする（チェックボックス）

任意のHTTPヘッダーを付与する項目も、bodyへ任意フィールドを追加する項目も無い。これは
Zoho CRM Notifications APIで発覚したのと同種の制約（`docs/zoho_webhook_activation_note.md`
「解決済み: Zoho通知はHTTPヘッダーでの認証をサポートしない」参照）だが、Zohoと異なり
kintoneはbody内に任意のtokenフィールドを追加させる仕組みも無い（bodyは常にレコードの
生データ）。そのため、**Webhook URL自体にクエリパラメータとして共有シークレットを埋め込む**
方式にした（`verify_webhook_query_param()`、`src/sync_engine/webhook_handlers/_common.py`）。
Slackの Incoming Webhook自体がURLにシークレットを埋め込む方式であり、「Webhook URLしか
設定できない」連携先向けの標準的な妥協点として扱う。

## 手順

1. Webhookシークレットを発行する（未発行の場合のみ、Zoho用と同じスクリプトを流用）。

   ```
   python scripts/generate_webhook_secret.py
   ```

2. 発行した値を **Vercel本番環境変数** `KINTONE_WEBHOOK_SECRET` に設定する。

3. kintone側で対象の3アプリ（取引先マスタ／案件管理／アクション管理。
   `src/sync_engine/webhook_handlers/kintone_webhook.py`の`_APP_ID_ENV_VARS`参照）
   それぞれについて、アプリ設定 → カスタマイズ/サービス連携 → Webhook で以下を設定する
   （**アプリごとに個別設定が必要**、3アプリ分繰り返す）。

   - Webhook URL:
     `https://crm-sfa-integration.vercel.app/api/webhooks/kintone?secret=<手順2で設定した値>`
   - 通知を送信する条件: 「レコードの編集」にチェック（「レコードの追加」は
     `Dispatcher.dispatch()`が新規レコード作成に未対応のため付けても意味が無い。
     `dispatcher.py`のコメント「新規レコード作成フローは本ディスパッチャのスコープ外」参照）
   - このWebhookを有効にする: チェック

4. **本番で有効化する前に、実際のkintoneレコード1件のフィールド値表記を確認する**（要検証、
   下記「既知の未検証事項」参照）。特に「契約進捗状況」の「商談中（A〜D）」の括弧が
   全角か半角か。`GET /k/v1/record.json`（kintone REST API、APIトークン方式）で実際に
   1レコード取得するか、Webhookを一時的に有効化してVercelのfunction logsで実際のペイロード
   を確認する。

5. 有効化後の動作確認チェックリスト。

   1. kintoneで対象アプリ（取引先マスタ／案件管理／アクション管理のいずれか）のレコードを
      1件編集する（`KINTONE_FIELD_TRANSFORMS`に載っているフィールド、例えば案件管理の
      「契約進捗状況」を変更する）。
   2. Vercelのfunction logsで `/api/webhooks/kintone` への200レスポンスを確認する。
   3. 同ログで`kintone webhook: ignoring field code=...`（意図的除外・未対応フィールド）や
      `kintone webhook: failed to transform field code=...`（値変換失敗）のwarning/infoログが
      想定通りかを確認する。
   4. 対応するNotionページのプロパティが更新されたか確認する。

## 既知の未検証事項

- **「商談中（A〜D）」等の選択肢値の括弧が全角か半角か**: `src/migration/project_mapping.py`の
  `_STATUS_ALIASES`は一括移行時の実CSVエクスポートで確認済みの全角括弧
  （`"商談中（B）"`）を使っている。`KINTONE_FIELD_TRANSFORMS`（`kintone_field_transforms.py`）は
  この関数（`normalize_project_status`）をそのまま再利用しているため、同じ表記を前提にしている。
  CSVエクスポートとWebhook/REST APIは別の取得経路であり、`normalize_date`が過去に
  「kintone CSVは`2023/12/01`、Notion APIはISO 8601が必要」という経路差異バグを踏んだ前例が
  あるため、選択肢の文字表記についても本番投入前に実データで確認すること。もし実際が
  半角括弧だった場合、`_STATUS_ALIASES`（および呼び出し元）の修正が必要になる。
- **`GET /k/v1/record.json`のNUMBER型フィールドの実際の返却型**: `kintone_field_transforms.py`の
  金額フィールド変換（`float(v) if v not in (None, "") else None`）は、kintoneのNUMBER型が
  文字列で値を返すという前提（`kintone_webhook.py`のモジュールdocstring記載のペイロード例が
  この前提）に基づく。この前提自体は変更していないが、Webhook経由の実際のペイロードで
  再確認しておくとより確実。

## 対象外フィールドの挙動に関する注意（運用者向け）

- kintoneのWebhook通知は、変更されたフィールドだけでなく**レコード全体の現在値**を含む
  （`kintone_webhook.py`のモジュールdocstring参照）。そのため、`KINTONE_FIELD_TRANSFORMS`の
  対象外フィールド（例: 案件管理の「施設名（会社名）」、アクション管理の「対応者」
  「担当者名」）だけを編集した場合でも、**Webhookイベント自体は発火し、対象内の他フィールド
  （営業ステータス・初期費用等）が変更されていない現在値で再度Notionへ書き込まれる**
  （冪等な上書きのため実害は無いが、「対象外フィールドの編集は完全に何も起きない」という
  誤解をしないよう注意。obasan-qualityレビューWARN対応、2026-08-14）。
- リレーション解決が必要なフィールド（取引先マスターへのリレーション・担当営業・
  先方担当者・提案サービス等）や派生値フィールド（取引先マスターDBの「営業ステータス」）は、
  kintone側で編集してもNotion側には一切反映されない（意図的な制約、
  `kintone_field_transforms.py`のモジュールdocstring参照）。

## 背景・設計メモ

`src/sync_engine/webhook_handlers/kintone_webhook.py` は以前から実装済みだったが、
「kintoneは他ツールからの書き込み専用」という方針のため実運用では有効化されていなかった。
2026-08-14、金沢さんの確認によりkintone→Notion方向も有効化する方針となった。

有効化前の調査で、既存実装（kintoneのフィールドコードをそのままNotionプロパティ名として
扱う素朴な実装）には重大な欠陥が見つかった。実際のkintoneフィールドコードはNotion
プロパティ名と一致しないことが多く（一括移行時の実データ検証済みコード
`src/migration/project_mapping.py`等で判明）、有効化してもほぼ全プロパティが
Dispatcher側で「スキーマに存在しない」として黙ってスキップされ、
リアルタイム反映が実質機能しないまま「設定済み」に見えてしまうところだった。これは
2026-08-12にZoho側で実際に発覚した本番障害
（`src/sync_engine/webhook_handlers/zoho_field_transforms.py`参照）と全く同じクラスの
問題であり、その教訓を活かして先に`kintone_field_transforms.py`
（`KINTONE_FIELD_TRANSFORMS`）を整備してから有効化する運びとした。

対象は取引先マスタ／案件管理／アクション管理の3アプリのみ（既存の
`config/.env.example`の`KINTONE_API_TOKEN_*`/`KINTONE_APP_ID_*`と対応するアプリと同じ）。
リレーション解決・派生値計算・DBをまたぐ反映が必要なフィールドは、Webhookの
1レコード単位の部分更新イベントでは同期的に解決できないため意図的に対象外としている
（詳細は`kintone_field_transforms.py`のモジュールdocstring参照）。
