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

4. **設定を保存したら、必ずアプリの「アプリを更新」ボタンを押す**（2026-08-14実際に
   ハマった落とし穴。Webhook設定画面での保存はkintoneの設定編集領域に留まり、
   別途アプリ全体を「更新」して初めて本番環境に反映される。これを踏まずに何度
   レコードを編集してもWebhookは一度も発火しない＝kintone側の「ログを確認」に
   何も表示されない状態になる）。

5. 有効化後の動作確認チェックリスト。

   1. kintoneで対象アプリ（取引先マスタ／案件管理／アクション管理のいずれか）のレコードを
      1件編集する（`KINTONE_FIELD_TRANSFORMS`に載っているフィールド、例えば案件管理の
      「契約進捗状況」＝実フィールドコード`ドロップダウン_2`を変更する）。
   2. Vercelのfunction logsで `/api/webhooks/kintone` への200レスポンスを確認する。
   3. 同ログで`kintone webhook: ignoring field code=...`（意図的除外・未対応フィールド）や
      `kintone webhook: failed to transform field code=...`（値変換失敗）のwarning/infoログが
      想定通りかを確認する。
   4. 対応するNotionページのプロパティが更新されたか確認する。
   5. kintone側の「Webhookログを確認」でも「エラー」ではなく実行された記録が残っているか
      確認する（アプリ設定 → Webhook → 対象行の「ログを確認」）。

## フィールドコードの検証方法（`scripts/list_kintone_fields.py`）

2026-08-14に実際に有効化した際、`KINTONE_FIELD_TRANSFORMS`のキーがCSV移行データの列名
（≒表示ラベル）を前提にしており、実際のフィールドコードと大きく食い違っていたことが
判明した（下記「背景・設計メモ」参照）。今後この対応表を見直す・新しいdb_keyを追加する
際は、必ず実際のフィールドコードを確認してから書くこと。

```
set -a; source config/.env; set +a
python scripts/list_kintone_fields.py --db-key project
```

コード・ラベル・型の一覧が表示され、コードとラベルが異なるフィールドには
`<-- コード!=ラベル`マークが付く（トークン自体は出力に含まれない）。

## 既知の未検証事項

- **「商談中（A〜D）」等の選択肢値の括弧の全角/半角**: 2026-08-14、金沢さん指摘対応で解決済み。
  `normalize_project_status()`（`src/migration/project_mapping.py`）が半角括弧を全角へ
  正規化してからエイリアス表を引くため、CSV移行データ（全角）・Webhook/REST API経由の
  実データのいずれでも同じ結果になる（`tests/migration/test_project_mapping.py`の
  `test_normalize_project_status_accepts_half_width_brackets`で回帰確認済み）。
- **`GET /k/v1/record.json`のNUMBER型フィールドの実際の返却型**: 2026-08-14、実際の
  kintone Webhook通知（本番）で確認済み。文字列で値が返ることを確認し、
  `kintone_field_transforms.py`の金額フィールド変換（`float(v) if v not in (None, "") else None`）
  はこの前提のまま問題ない。

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
プロパティ名と一致しないことが多く、有効化してもほぼ全プロパティがDispatcher側で
「スキーマに存在しない」として黙ってスキップされ、リアルタイム反映が実質機能しないまま
「設定済み」に見えてしまうところだった。これは2026-08-12にZoho側で実際に発覚した本番障害
（`src/sync_engine/webhook_handlers/zoho_field_transforms.py`参照）と全く同じクラスの
問題であり、その教訓を活かして先に`kintone_field_transforms.py`
（`KINTONE_FIELD_TRANSFORMS`）を整備してから有効化する運びとした。

対象は取引先マスタ／案件管理／アクション管理の3アプリのみ（既存の
`config/.env.example`の`KINTONE_API_TOKEN_*`/`KINTONE_APP_ID_*`と対応するアプリと同じ）。
リレーション解決・派生値計算・DBをまたぐ反映が必要なフィールドは、Webhookの
1レコード単位の部分更新イベントでは同期的に解決できないため意図的に対象外としている
（詳細は`kintone_field_transforms.py`のモジュールdocstring参照）。

### 実際に有効化して発覚した2つの問題（2026-08-14）

上記の対策（`KINTONE_FIELD_TRANSFORMS`整備）を済ませてから金沢さんに実際の有効化作業を
依頼したところ、想定していなかった2つの問題に順番にぶつかった。

**問題1: 「アプリを更新」を押していなかったため、Webhook自体が一度も発火しなかった**。
Webhook設定画面での保存・「このWebhookを有効にする」チェックは正しく行われていたが、
kintoneの仕様上、設定変更を本番環境へ反映するには別途アプリ全体の「アプリを更新」操作が
必要だった。これを踏むまでは、レコードをいくら追加・編集してもkintone側の「Webhookログ」
に一切記録が残らない（試みてすらいない）状態になり、原因の切り分けにかなりの時間を要した
（Vercel側のログ・kintone側の実行ログ・URLの文字列突き合わせ・kintoneプラン確認等、
考えられる原因を一通り除外した後に判明）。同じ現象に遭遇した場合、まずこれを疑うこと。

**問題2: `KINTONE_FIELD_TRANSFORMS`のキーがCSV移行データの列名（表示ラベル相当）を
前提にしており、実際のフィールドコードと全く別物だった**。問題1を解消して初めて届いた
本物のWebhook通知のログで、アクション管理アプリの実フィールドコードが
`actionContent`/`comment`/`cnctorMember`/`toPerson`/`service`/`client_name`/
`nextActionDate`という英語ベースの識別子であることが判明し、当初の日本語ラベルベースの
キー（「アクション内容」「コメント」等）が一切マッチせず、対象フィールドが全て
「対象外」としてスキップされていたことが分かった。`config/.env`のkintone APIトークンで
`GET /k/v1/app/form/fields.json`を実際に呼び出し（`scripts/list_kintone_fields.py`として
恒久化）、3アプリ全てのコード・ラベル対応を突き合わせて`KINTONE_FIELD_TRANSFORMS`を
修正した。特に案件管理アプリの金額フィールドは、コード`初期費用`のラベルが
「提案料金（イニシャル）」、コード`初期費用_0`のラベルが「提案料金（ランニング）」という、
コード文字列とラベルの対応が直感に反する組み合わせだったため注意（詳細は
`kintone_field_transforms.py`のコメント参照）。

この2件の教訓: **CSVエクスポートの列名・kintone管理画面の表示ラベル・実際のフィールド
コードは、この環境では三者三様であり、どれか1つから他を推測してはいけない**。今後
`KINTONE_FIELD_TRANSFORMS`を変更・拡張する際は、必ず`scripts/list_kintone_fields.py`で
実コードを確認すること。
