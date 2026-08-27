# ツール間リレーション同期（ClientNameIndex/RelationReviewQueue）有効化手順

## 前提

kintone等の他ツールから届く自由入力の会社名テキスト（例: アクション管理の`client_name`）を、
Notionの取引先マスターDBへのリレーションへ同期的に解決するための仕組み（`src/relation_sync/`、
2026-08-25）。

- `ClientNameIndex`（Postgres）: 取引先マスターDB（Notion）の「正規化した取引先名」→
  「Notion page ID」の高速検索用インデックス。データの正本は引き続きNotionであり、双方向
  同期パイプライン（Dispatcher/SyncEvent/IdMappingStore）には一切関与しない
  （`src/project_mirror/`の`ProjectMirror`と同じ設計思想）。
- `RelationReviewQueue`（Postgres）: リレーション解決が曖昧（候補0件・複数件）だった場合に、
  自動確定させず人の確認待ちとして記録するキュー。
- `src/relation_sync/resolve.py`の`resolve_client_master_relation()`が上記2テーブルを使って
  実際の解決を行い、`src/sync_engine/webhook_handlers/kintone_field_transforms.py`の
  `KINTONE_FIELD_TRANSFORMS["action"]["client_name"]`から呼ばれる。

インフラ（Prismaマイグレーション・Pythonモジュール・Webhookフック・cron登録）は既に
コードとして存在するが、`RELATION_SYNC_ENABLED`環境変数が未設定（既定`false`）である限り、
本番挙動は一切変わらない。

## `RELATION_SYNC_ENABLED`が制御する範囲（重要: ProjectMirrorとの設計の違い）

`PROJECT_MIRROR_SYNC_ENABLED`は「書き込み系（Webhook反映・夜間reconciliation cron）」だけを
制御し、読み取り側（`resolve_client_master_relation()`相当の処理）は存在しない
（ProjectMirrorはダッシュボード表示用の読み取り専用ミラーであり、読み取り元の切り替えは別途
`PROJECT_MIRROR_READ_ENABLED`が担う）。

一方`RELATION_SYNC_ENABLED`は以下3箇所すべてを制御する（`resolve_client_master_relation()`
自身がフラグを見る点がProjectMirrorと異なる、`src/relation_sync/resolve.py`のモジュール
コメント参照）:

1. `POST /api/webhooks/notion`経由のリアルタイム同期（`sync_client_name_to_index`、
   `build_client_name_index_sync_callable`参照）。
2. 夜間reconciliation cron（`GET /api/cron/relation-sync-reconcile`）。
3. **`resolve_client_master_relation()`自体**（`src/sync_engine/webhook_handlers/
   kintone_field_transforms.py`の`KINTONE_FIELD_TRANSFORMS`テーブルから常時・フラグ無しで
   呼ばれる同期応答経路に組み込まれているため）。

3番目が必要な理由: kintoneのアクション管理Webhookは`client_name`フィールドが変更されるたびに
`resolve_client_master_relation()`を無条件で呼ぶ。もし1・2だけをフラグで止めて
`resolve_client_master_relation()`自体を素通りさせると、`ClientNameIndex`が永久に空のまま
（1・2が無効化されているため）取引先名の検索を試み続け、**正しい取引先名であっても常に
「候補なし」と誤判定され、`RelationReviewQueue`へ無意味なエントリが積み上がり続ける**
（インフラ整備だけの段階でこれが起きると「インフラ整備のみでは本番挙動は変わらない」という
前提が崩れる）。そのため`resolve_client_master_relation()`自身が`RELATION_SYNC_ENABLED`を
確認し、無効時は`ClientNameIndex`への問い合わせも`RelationReviewQueue`への記録も一切行わず
`None`を返す（kintone側は「未解決」として扱い、既存のNotionリレーション値を変更しない）。

## ロールアウト順序

### 1. インフラ整備をデプロイ

`RELATION_SYNC_ENABLED`未設定（`false`）のまま、Prismaマイグレーション・
`src/relation_sync/`・kintone Webhookの`client_name`解決配線・Notion Webhookフック・
cron登録一式をデプロイする。本番挙動は変化しない（上記の通り、resolve自体もno-opのまま）。

### 2. 初回バックフィル

`scripts/backfill_client_name_index.py`を実行し、Notion取引先マスターDB全件を
`ClientNameIndex`へ初回投入する（読み取り専用のNotion API呼び出し + 自インデックスへの
UPSERTのみで、Notion本番データを一切変更しない）。

```
set -a; source config/.env; set +a
python scripts/backfill_client_name_index.py
```

`DATABASE_URL`（Neon Postgres接続文字列）・`NOTION_API_KEY`が必要。出力に`synced_count`
（UPSERT件数）・`deleted_count`（今回のバッチで削除されたstale行数、初回は通常0）が
表示される。取引先マスターDBは約1万件規模のため、`synced_count`が0件、または100件未満の
ように極端に少ない場合は成功として扱わず、Notion API権限不足等の失敗を疑うこと。

### 3. `RELATION_SYNC_ENABLED=true`

Vercel本番環境変数に`RELATION_SYNC_ENABLED=true`を設定する。これにより上記「制御する範囲」
の3箇所すべてが有効化される（ProjectMirrorのような「書き込みだけ先に有効化し、読み取りは
後で切り替える」という2段階は存在しない。1本のフラグでリレーション解決全体がON/OFFされる）。

有効化後、kintoneのアクション管理で`client_name`が編集されるたびに、`ClientNameIndex`を
検索して1件だけヒットすれば⑥アクション履歴DBの「👨‍👩‍👧‍👦 取引先マスター」リレーションへ
自動反映される。曖昧（0件・複数件）だった場合は`RelationReviewQueue`へ記録されるだけで、
Notion側のリレーションは変更されない。

**既存のNotionリレーションは黙って上書きしない**（2026-08-25、GPT-5.6クロスレビュー指摘
対応）: 自動解決が完全一致でNotion page IDを得られた場合でも、対応するNotionページの
「👨‍👩‍👧‍👦 取引先マスター」プロパティに**既に何か値が設定されていれば書き込まない**
（`kintone_webhook.py`の`_drop_client_master_relation_if_already_set`参照）。これが無いと、
人がNotion上で手動修正したリレーションが、後日kintone側の`client_name`が再編集される
たびに黙って上書きされてしまい、「静かな誤紐付けを避ける」という本機能の目的そのものに
反する動作になる。自動反映は「Notion側がまだ未設定の場合のみ」に限定される。現在値の確認
（`ProductionSyncWiring.id_mapping_store`での逆引き＋`any_db_page_client.get_page()`）に
失敗した場合も、安全側に倒して当該プロパティへの書き込みをスキップする。

### 4. RelationReviewQueueの定期確認

`scripts/list_relation_review_queue.py`を実行し、pending状態のレビュー項目を確認する
（読み取り専用、書き込みは一切行わない）。

```
set -a; source config/.env; set +a
python scripts/list_relation_review_queue.py
```

**確認頻度の目安**: 有効化直後の1〜2週間は、kintone側の会社名表記ゆれ・
`normalize_company_name_strong()`が吸収しきれないパターンが多数見つかる想定のため、週1回
程度の頻度で確認し、必要に応じて`ClientNameIndex`側（Notion取引先マスターDBの表記）または
kintone側の入力を修正する。安定してからは月1回程度でよい。

pending項目をresolved/dismissedにする専用の仕組みは今回のスコープでは用意していない
（手動SQL操作を前提とする最小実装、`src/relation_sync/review_queue.py`参照）。例:

```sql
UPDATE "RelationReviewQueue"
SET status = 'resolved', "resolvedAt" = now(), "resolvedNotionPageId" = '<Notion page ID>'
WHERE id = '<row id>';
```

### 5. ロールバック

問題があれば`RELATION_SYNC_ENABLED=false`に戻すだけで即座に無効化できる（コード変更不要）。
`ClientNameIndex`/`RelationReviewQueue`への書き込みが止まるだけで、既に⑥アクション履歴DBへ
反映済みのリレーション値（Notion側）はそのまま残る（ロールバックでは削除・巻き戻しされない）。

## 多重実行防止（アドバイザリロック）

`refresh_all_client_names()`（バックフィルスクリプト・夜間reconciliation cron共通）は、実行
開始時にPostgresアドバイザリロック（`pg_try_advisory_lock`、`src/relation_sync/db.py`の
`try_acquire_refresh_lock`/`release_refresh_lock`。ロックキーは`ProjectMirror`用と衝突しない
別の値を使う）を試みる。夜間cronと手動バックフィルの実行が偶発的に重なった場合、後から取得を
試みた側は`{"skipped": "already_running"}`を返して即座にスキップする。

## RelationReviewQueueの重複防止（部分ユニークインデックス）

同一の(`sourceTool`, `sourceRecordId`, `targetDbKey`, `rawValue`)の組み合わせでpending状態の
行が既に存在する場合、`enqueue_for_review()`は`INSERT ... ON CONFLICT ... DO NOTHING`で
DB側から原子的にスキップする（`status = 'pending'`の行のみを対象とする部分ユニークインデックス
`RelationReviewQueue_pending_dedupe_key`を使う。Prismaのスキーマ定義言語では部分インデックスを
表現できないため、`dashboard/prisma/schema.prisma`上には現れず`migration.sql`にのみ存在する。
詳細は同ファイルのコメント参照）。resolved/dismissedになった行は対象外のため、一度レビュー
済みの組み合わせが後日再び曖昧になった場合は新規に積み直される。

## 過去のインシデント: mark-and-sweepの精度不一致による誤削除（2026-08-25）

`upsert_client_names_and_sweep()`が基準時刻に素の`datetime.now(timezone.utc)`（マイクロ秒精度）を
使っていたところ、Postgresの`TIMESTAMP(3)`カラムへの保存時に四捨五入（round-half-up、境界に
よっては繰り上がる）でミリ秒精度へ丸められる一方、末尾のDELETEのWHERE比較には丸められて
いない元の値がそのまま使われ、丸め方向次第で`保存値 < 比較用の元の値`が真になり、今まさに
挿入したばかりの行まで誤って削除される事故になりうる不具合だった（`src/project_mirror/db.py`の
`upsert_projects_and_sweep()`で同一パターンが実際に本番incidentとして発生し、`ProjectMirror`が
0件になってダッシュボードの案件管理画面が空表示になった。詳細は`docs/project_mirror_activation_
note.md`の同名セクション参照）。`ClientNameIndex`側は本番incident発生前に同じ実装パターンとして
発見・修正済み。

対策として、基準時刻の計算を`src/db_utils.py`の`db_truncated_utcnow()`に置き換えた（マイクロ秒を
事前に1000の倍数＝ミリ秒境界へ切り捨てておくことで、Postgres側の丸めが切り捨てでも繰り上げでも
結果が変わらない不動点にする）。

**次に同種のmark-and-sweep方式のミラーテーブル（全件UPSERT→`"syncedAt" < 基準時刻`でDELETEする
パターン）を新設する際は、必ず`src/db_utils.py`の`db_truncated_utcnow()`を使うこと**（素の
`datetime.now(timezone.utc)`を使うと、DBの精度丸めとの不一致で投入直後の行が誤削除される事故が
過去に発生した）。

## 動作確認チェックリスト（各ステップ共通）

1. Vercelのfunction logsで対象エンドポイント（Webhook/cron）の200レスポンスを確認する。
2. `scripts/backfill_client_name_index.py`または夜間cronのレスポンス（`synced_count`/
   `deleted_count`）が想定件数と大きく乖離していないか確認する。
3. Neon Postgres側で`SELECT count(*) FROM "ClientNameIndex";`を実行し、Notion側の取引先
   マスター件数と概ね一致するか確認する。
4. `scripts/list_relation_review_queue.py`でpending件数が異常に多くないか確認する
   （多い場合は名寄せロジック・kintone側入力の見直しを検討する）。

## 既知の制約・将来の検討事項（2026-08-25、GPT-5.6クロスレビュー指摘の記録）

対応不要と判断したが記録として残しておく指摘事項:

- **一度自動解決した後の経年劣化**: 一度完全一致で自動解決してNotionのリレーションへ反映した
  後、別の取引先が取引先マスターに追加されて同じ正規化名が曖昧（複数候補）になった場合、
  過去に自動解決済みのリレーションは再チェックされず古いまま残る（誤りではなく、当時
  確定した情報のスナップショットとして残り続ける）。今回のスコープでは再検証の仕組みは
  設けていない。
- **Webhookの到着順序**: kintone Webhookが発生順どおりに届くとは限らないため、
  `resolve_client_master_relation()`は常にkintone側の最新状態を都度取得するのではなく、
  届いたWebhookペイロードに含まれる値をそのまま使う設計上の前提がある（他の
  `KINTONE_FIELD_TRANSFORMS`エントリと同じ、Webhookペイロード単体で完結する設計）。
- **正規化ロジック変更時の整合性**: `normalize_company_name_strong()`
  （`src/migration/zoho_client_master.py`）を将来変更した場合、既存の`ClientNameIndex`は
  旧ロジックで正規化されたキーのまま残るため、新ロジックとの整合性が崩れる。正規化ロジック
  を変更した場合は`scripts/backfill_client_name_index.py`で`ClientNameIndex`を全件
  再構築すること。
- **承認済みエイリアス辞書**: 表記ゆれが大きい別名（例: サイボウズ/株式会社サイボウズ/
  Cybozu等、`normalize_company_name_strong()`の全角半角統一・法人格表記ゆれ吸収だけでは
  同一と判定できないケース）を人が事前に承認した上で紐付けられる「承認済みエイリアス辞書」
  のような仕組みは、今回未実装。`RelationReviewQueue`の運用が実際に回り始め、この種の
  ケースが頻出するようであれば将来の検討候補とする。

## Round2（2026-08-25）: Zoho側の解決・Dispatcherの新規レコード作成

Round1（本ドキュメントの本編、kintoneアクション履歴の`client_name`解決）に続き、以下2点を
`RELATION_SYNC_ENABLED`を流用したまま追加した（新規フラグは1つのみ追加、後述）。

### Zoho「取引先」/「【Notion】取引先マスター」の解決

Zohoのアクション履歴モジュール（CustomModule2）で`field6`（取引先、生の会社名）または
`field22`（【Notion】取引先マスター、移行時にNotionページへの直リンクが埋め込まれている
ことがある自由記述）が変更された場合、`src/relation_sync/resolve_zoho.py`の
`resolve_zoho_action_client_master_relation()`が以下の優先順位で解決する:

1. `field22`に埋め込まれたNotionページIDヒント（`src/migration/_utils.py`の
   `extract_notion_page_id()`）があれば、そのまま使う（名寄せ不要、最も信頼性が高い）。
2. 無ければ`field6`の生の会社名を、Round1と同じ`resolve_client_master_relation()`に渡して
   名寄せする（完全一致のみ自動、曖昧なら`RelationReviewQueue`へ、既存の安全設計を踏襲）。

`field22`/`field6`のどちらか一方しかWebhook通知の変更差分（delta）に含まれない場合、
もう一方の現在値はZoho API（`get_record`）でレコード全体を取得して補う。この機能全体も
`RELATION_SYNC_ENABLED`でガードする（未設定時はZoho APIへの追加問い合わせも一切行わない）。

kintone側と同じ「後勝ち」上書き防止ガード（Notion側に既に値が設定されていれば自動解決の
結果があっても上書きしない）は、`src/sync_engine/webhook_handlers/_relation_guard.py`へ
ツール非依存の形で切り出し、kintone/Zoho両Webhookハンドラで共有している。

「案件」(project)リレーションはkintone側と同じ理由（案件を一意に特定できる情報が無く、
自動選択はもちろんレビューキューへ積んでも人間が判断できる材料が無い）で今回もスコープ外。

### Dispatcherの新規レコード作成（`unknown_record`スキップの解消）

`src/sync_engine/dispatcher.py`の`Dispatcher.dispatch()`は、`IdMapping`が見つからない
（＝対応するNotionページが未作成の）kintone/Zoho発イベントを従来`unknown_record`として
即座にスキップしていたが、新しい環境変数`AUTO_CREATE_NEW_RECORDS_ENABLED`（既定`false`）を
有効にすると、レコード全体データを取得しNotionに新規ページを作成するようになる
（`Dispatcher._try_create_new_record()`参照）。

`RELATION_SYNC_ENABLED`とは意図的に別変数にした: 新規ページ作成は既存プロパティの更新より
「間違えた場合の実害が大きい」（重複ページ・不完全なページの量産リスク）ため、独立して
ON/OFF・段階導入できるようにする（`PROJECT_MIRROR_SYNC_ENABLED`/`PROJECT_MIRROR_READ_ENABLED`
の分離と同じ設計思想）。

処理の流れ:

1. `event.source_tool`（kintone/Zohoのみ対象。Notion・スプレッドシートは対象外）の
   `SyncTarget.get_record()`でレコード全体データを取得する。
2. `src/sync_engine/new_record_builder.py`の`build_notion_properties_for_new_record()`が、
   既存のWebhook部分更新用1フィールド単位変換テーブル（`KINTONE_FIELD_TRANSFORMS`/
   `ZOHO_LABEL_FIELD_MAPPINGS`、上記の取引先マスターリレーション解決も含む）をレコード全体の
   各フィールドへループ適用し、Notionプロパティへ変換する。
3. 対象db_key（`get_schema(event.db_key)`）の`RequirementLevel.REQUIRED`なプロパティが
   変換後のデータに1つでも欠けていれば、不完全なページを作らずスキップする（**どのdb_keyで
   実際に発火するか・常にスキップされるかは下記「新規作成が実際に機能するdb_key一覧」参照**）。
4. Notionページ作成の直前にもう一度`IdMapping`の有無を確認し（下記「重複作成の防止」参照）、
   Notion側に新規ページを作成する。
5. `IdMappingStore.upsert(mapping, expected_last_synced_at=None)`（新規作成を期待するCAS）で
   新しいマッピングを登録する（`last_synced_at`は当該イベントの`occurred_at`をそのまま使う）。
   一時的な障害に備えて数回リトライし、それでも失敗した場合は下記「重複作成の防止」の
   補償アクション・アラートが動く。

案件(project)のような紐付け先を特定できないリレーションは、変換テーブルにエントリが
存在しないため、新規作成時も自然に空欄のまま作成される（上記Round1・Zoho解決と同じ方針）。

### 新規作成が実際に機能するdb_key一覧（2026-08-25、shirokuma-sec/obasan-qualityレビューBLOCKER対応）

`RequirementLevel.REQUIRED`なプロパティと、各`KINTONE_FIELD_TRANSFORMS`/
`ZOHO_LABEL_FIELD_MAPPINGS`テーブルを実際に突き合わせた結果、**新規作成が発火する
db_key・発火元ツールの組み合わせは限られている**（安全側に倒した結果であり不具合ではないが、
「`AUTO_CREATE_NEW_RECORDS_ENABLED=true`にしたのに新規ページが全く作られない」という
一見バグに見える状態が正常であることが分かるよう明記する）。

| db_key（DatabaseSchema.key） | kintone発イベント | Zoho発イベント |
|---|---|---|
| `client_master`（①取引先マスター） | ✅ 機能する（「顧客名」→「取引先名」で必須のtitleを満たせる） | ✅ 機能する（「取引先名」→「取引先名」） |
| `project`（④案件管理） | ❌ 常にスキップ（`KINTONE_FIELD_TRANSFORMS["project"]`に必須titleプロパティ「案件名」に対応するkintoneフィールドが無い） | ✅ 機能する（「案件名」→「案件名」、「ステージ」→「営業ステータス」の両必須プロパティを満たせる） |
| `action`（⑥アクション履歴） | ❌ 常にスキップ（必須titleプロパティに対応するkintoneフィールドが無い） | ❌ 常にスキップ（必須プロパティ「アクション種別」に対応するZohoラベルのマッピングが無い。「アクション名」→titleは満たせるが片方不足で作成されない） |
| `chain`（⑤チェーン） | （kintoneはchainを同期対象としないため、そもそもkintone発イベント自体が発生しない） | ✅ 機能する（「チェーン名・グループ名」→「グループ名」） |
| `contact`（②連絡先） | （同上、kintoneはcontactを同期しない） | ❌ 常にスキップ（必須プロパティ「取引先マスター」リレーションに対応するZohoラベルのマッピングが無い） |
| `product`（③サービス・商品） | （同上、kintoneはproductを同期しない） | ❌ 常にスキップ（必須プロパティ「課金形態」に対応するZoho列が存在しない。移行時も既定値を書き込んでいるだけで導出元が無い） |

要点:
- **kintoneは`client_master`のみ**（`project`/`action`は共にtitleプロパティを導出できず常に
  スキップ）。kintoneはそもそも`chain`/`contact`/`product`を同期対象としていないため
  （`kintone_webhook.py`の`_APP_ID_ENV_VARS`参照）、これら3db_keyのkintone発イベント自体が
  発生しない。
- **Zohoは`client_master`/`project`/`chain`の3db_key**（`action`/`contact`/`product`は
  必須プロパティのうち1つ以上が変換テーブルに存在せず常にスキップ）。
- 対応するkintone/Zoho側フィールドが将来追加・整備された場合は、この表を更新すること
  （`KINTONE_FIELD_TRANSFORMS`/`ZOHO_LABEL_FIELD_MAPPINGS`のコメントも合わせて参照）。

### 重複作成の防止（2026-08-25、shirokuma-sec/obasan-qualityレビューBLOCKER対応、同日の
最終レビューで追加のBLOCKERを1件対応）

`notion_target.upsert_record()`（Notionページ作成）が成功した直後に`IdMapping`登録
（`IdMappingStore.upsert()`）が失敗すると、Notionページは作成済みなのにマッピング未登録の
まま処理が終わり、kintone/Zoho側の自動リトライや真の並行Webhookで同じイベントが再処理
された場合、`IdMapping`が依然見つからず**同じレコードに対応するNotionページが重複作成
されうる**。`Dispatcher._try_create_new_record()`は以下で対応している:

1. **再確認**: Notionページ作成の直前にもう一度`_resolve_mapping()`でmappingの有無を
   確認する（レース窓を縮める。完全な排他制御ではない）。
2. **`create_page()`呼び出し自体の失敗を安全側に倒す**（最終レビューBLOCKER対応）:
   サーバーレス環境ではNotion APIへのPOST自体は成功したがレスポンス受信前にタイムアウト/
   接続断/5xxが発生し、`notion_target.upsert_record()`が例外を送出するケースが現実的に
   起こりうる。**この例外を捕捉せず呼び出し元へ伝播させると、Webhookハンドラの広い
   `except Exception`が500を返し、kintone/Zoho側のリトライで`mapping`が依然Noneのまま
   本メソッドへ再突入し、保護ロジックを一切経由せずに重複ページ作成が再現してしまう**
   （Round2が最初から防ごうとしていた問題そのもの）。そのため`Dispatcher`はこの例外を
   `_handle_uncertain_notion_page_creation()`で捕捉し、**Webhookレスポンスとしては200
   （受理済み・リトライ不要）を返す**一方、ページが実際に作成されたか不明である旨を
   Slackへ明確に伝え、人による手動確認を促す（自動で危険な推測をするより、人が確認できる
   形で安全側に倒すという、Round1の「完全一致のみ自動反映・曖昧なら要確認キューへ」と
   同じ設計思想）。このケースはページIDが分からない（Notion APIからの応答を受け取れて
   いない）ため、下記4の自動アーカイブは行えない。
3. **CASでの登録 + リトライ**: `IdMapping`登録は`IdMappingStore.upsert(mapping,
   expected_last_synced_at=None)`（新規作成を期待するcompare-and-swap）で行う
   （`Dispatcher._register_new_record_mapping()`参照）。
   - `DuplicateExternalIdError`（外部IDが既に別のnotion_keyに紐づいている＝真の並行作成に
     よる恒久的な失敗）は、リトライしても結果が変わらないため待機・リトライせず即座に
     諦める。
   - それ以外の例外（DB接続断・レート制限等の一時的な障害を想定）は、固定の短い待機
     （0.2秒、指数バックオフまでは導入していない。高々2回のリトライのため）を挟んで
     最大2回リトライする。
4. **補償アクション + アラート**: 上記3が最終的に失敗した場合、作成済みのNotionページを
   アーカイブする補償アクションを試み（`Dispatcher._handle_orphaned_notion_page()`）、
   成否によらず孤児ページのNotion page IDを含む明確なSlackアラートを出す（サイレントな
   500返却でリトライ→再度の重複作成を誘発させない設計）。
5. **自己修復（バックエンド依存、要注意）**: 真の並行実行で両者ともNotionページ作成に
   成功してしまった場合、`IdMappingStore.upsert()`は`db_key`単位で外部ID
   （kintone_id/zoho_id）の重複を検査する（`DuplicateExternalIdError`）ため、後勝ちの
   登録は多くの場合失敗し、そちら側のページが上記4の補償アクションでアーカイブされる。
   **ただしこの自己修復は`IdMappingStore`の実装に依存する**:
   - `SQLiteIdMappingStore`（ローカル開発・簡易本番用）はDBレベルのUNIQUE INDEXを持つため、
     事前チェックをすり抜けても最終的に確実に検知できる（belt-and-suspenders）。
   - `NotionIdMappingStore`（本番運用で使う想定のバックエンド、
     `src/sync_engine/notion_id_mapping.py`）は**自身のdocstringが明記する通りDBレベルの
     一意制約が無く**、重複検知は`upsert()`内の事前チェック（クエリでの検索）のみが
     唯一の防御線である。ほぼ同時に2つのWebhookが処理された場合、両方の事前チェックが
     「重複なし」と判定してしまい`DuplicateExternalIdError`を検知できないレース窓が
     残る（分散ロック等による解消は今回のスコープ外）。この場合、重複ページのどちらも
     アーカイブされないまま残る可能性があるため、下記「動作確認チェックリスト」・
     「ロールバック」の手動確認が重要になる。

それでも真の同時実行を完全には防げない（1のチェックと実際の`create_page()`の間にも短い
レース窓は残る）。Notion API側にリソース単位の悲観的ロック機構が無いため、これは既知の
残存リスクとして受け入れている（発生確率は極めて低く、発生しても4の補償アクション・
5の自己修復（バックエンドによっては不完全）で対処する設計）。

### 運用可視性（Slack通知）

新規レコード作成に関する以下5つのタイミングでSlackへ通知する:

- ✅ 新規ページ作成成功時（`notify_new_record_created`）
- ⚠️ 必須プロパティ不足によるスキップ時（`notify_new_record_issue`、
  `reason="missing_required_properties"`）
- 🚨 （最重要）`IdMapping`登録失敗時（`notify_new_record_issue`、
  `reason="mapping_registration_failed"`。孤児ページのNotion page IDを含む）
- 🚨 （最重要）Notion API呼び出し自体が例外で失敗し、ページ作成の成否が不明な時
  （`notify_new_record_issue`、`reason="notion_creation_status_unknown"`。Notion page IDは
  不明なため含まれない。手動でNotion側を直接確認する必要がある）
- 🚨 （最重要）元レコード（kintone/Zoho側）の取得自体が例外で失敗した時（`notify_new_record_issue`、
  `reason="source_record_fetch_failed"`。2026-08-27本番障害対応、下記「2026-08-27の本番障害と
  対応」参照。ページはまだ作成されていない）

**通知先（2026-08-25、送信方式変更）**: 当初は`SLACK_WEBHOOK_URL_ALERT`環境変数のIncoming
Webhook（`notify_conflict`と同じ「未設定なら無効化」パターン）を想定していたが、本番環境に
この変数が未設定であることが判明した。金沢さんへの確認の結果、「まずは金沢のDMへ、
ゆくゆくはマネージャー陣のDMへ」という方針になったため、`src/incident_detection/notify.py`
（高優先度インシデント検知の即時通知）と同じ「`User.isManager = true`の全ユーザーへSlack DM」
方式（`src/notifications/manager_dm.py`、既存の`SLACK_BOT_TOKEN`を使用、新規env変数なし）に
変更した。通知先はハードコード/env変数ではなく、dashboard管理画面でON/OFFできる
`User.isManager`フラグから都度動的に解決するため、マネージャー陣への拡大時もコード変更は
不要（対象ユーザーの`isManager`をONにするだけでよい）。なお`notify_conflict`（Round1、
コンフリクト自動解決通知）は本項の対象外で、引き続き`SLACK_WEBHOOK_URL_ALERT`のままである。

**`WebhookSlackNotifier`自体はSlackへの送信に失敗しても例外を投げない**（3回目最終レビュー
BLOCKER対応、2026-08-25。DM送信方式への変更後も踏襲）: 上記4つの通知のうち特に🚨2件は
「他の保護ロジックが失敗した後の最終防衛線」として呼ばれるため、通知処理自体
（Slackユーザー解決・`chat.postMessage`・`User`テーブルへのDB接続等）がタイムアウト等で
失敗した場合でもログに残すのみで静かに戻る実装にしてある。個別の呼び出し箇所
（`Dispatcher`側）でtry/exceptを重ねる必要はない（Notifier自体が「絶対に失敗しない」実装に
なっているため）。

**DM本文に人間向け表示名・対処アクションを埋め込む**（2026-08-25、動物チーム
（shirokuma-sec/obasan-quality/kuma-qa）レビュー対応）: DM本文の`DB:`行には`db_key`の生値
（例: "client_master"）に加えて`src/db_schema/registry.py`の`get_schema(db_key).display_name`
（例: "取引先マスターDB"）を併記する。`notify_new_record_issue`ではさらに、`reason`ごとの
一言アクションを`対応:`行として追加する（`src/sync_engine/slack_notifier.py`の
`_ISSUE_REASON_ACTION_HINTS`）。深夜・休日に届きうる緊急通知（特に🚨2件）でも、本ドキュメントを
別途参照しなくてもその場で次にすべきことが分かるようにするため。**このdictの文言は下記
「動作確認チェックリスト」の対処手順と一致させてある。どちらかを変更した場合は、もう一方も
忘れずに更新すること**（二重管理を避けるため、詳細な手順自体はこのドキュメント側に譲り、DM
本文には要点のみを埋め込む設計）。

**DM送信ループのタイムアウト予算**（2026-08-25、shirokuma-secレビュー【最重要】対応）:
`dispatcher.dispatch()`はkintone/Zoho Webhookハンドラから`BackgroundTasks`を使わず同期的に
呼ばれるため（本番はVercelのサーバーレス関数(FastAPI)としてデプロイされており、
レスポンス送信後に関数プロセスが凍結/終了しうるため`BackgroundTasks`は導入していない）、DM
送信の遅延がそのままWebhookレスポンスの遅延になり、kintone/Zoho側のリトライによる重複ページ
作成を誘発しかねない。`manager_dm.notify_managers()`はマネージャーN人ぶんのDM送信ループ全体に
合計5秒のタイムアウト予算（`_NOTIFY_MANAGERS_TIME_BUDGET_SECONDS`）を設け、超過したら残りの
マネージャーへの送信を打ち切り`logger.warning`で記録する。各Slack API呼び出し自体のtimeoutも
10秒から3秒（`_DM_API_CALL_TIMEOUT_SECONDS`）に短縮した。「1人への送信失敗が他の対象者への
送信を止めない」という既存の安全設計はそのまま維持している。

**`SLACK_BOT_TOKEN`未設定・マネージャー0人時のログ**（2026-08-25、shirokuma-secレビュー
対応）: 以前はこの2ケースでログすら残さず静かにreturnしていたため、上記「Round2のロール
アウト手順」2.で確認を促している`isManager`フラグの設定漏れが起きた場合、通知が届かない
だけでなく「なぜ届かなかったか」の痕跡も残らなかった。現在は`manager_dm.notify_managers()`が
両ケースとも`logger.warning`で1行残す。

### 自動作成されたページを監査ログで確認する方法

`HttpNotionClient.create_page()`は`record_notion_write(action="create", ...)`で
監査ログ（`AuditLog`テーブル、`dashboard/prisma/schema.prisma`のモデル定義・`src/audit_log/`）
へ書き込みを記録する（本機能専用の実装ではなく、既存の監査ログ記録がそのまま新規作成にも
適用される）。`src/audit_log/actor_context.py`の`set_actor()`はWebhookハンドラ側
（`kintone_webhook.py`/`zoho_webhook.py`の`handler()`）で`"kintone_webhook"`/`"zoho_webhook"`を
`actorSource`として設定しているため、**自動作成されたページは`AuditLog.actorSource`が
`"kintone_webhook"`または`"zoho_webhook"`、`action`が`"create"`のレコードとして残る**（人が
手動でNotion上に作成したページとは区別できる）。

確認クエリ例（実カラム名は`dashboard/prisma/schema.prisma`の`AuditLog`モデル定義で検証済み、
Neon Postgresを想定）:

```sql
-- 直近7日間に自動作成されたページ一覧（db_key別件数）
SELECT "dbKey", COUNT(*)
FROM "AuditLog"
WHERE action = 'create'
  AND "actorSource" IN ('kintone_webhook', 'zoho_webhook')
  AND "createdAt" > now() - interval '7 days'
GROUP BY "dbKey"
ORDER BY 2 DESC;

-- 個別レコードの詳細（Notion page ID・作成元Webhook・作成日時）
SELECT "notionPageId", "dbKey", "actorSource", "createdAt"
FROM "AuditLog"
WHERE action = 'create'
  AND "actorSource" IN ('kintone_webhook', 'zoho_webhook')
ORDER BY "createdAt" DESC
LIMIT 100;
```

### Round2のロールアウト手順（2026-08-25、shirokuma-sec/obasan-qualityレビューBLOCKER対応）

Round1（本ドキュメント冒頭「ロールアウト順序」）と同水準の手順。**前提条件として
`RELATION_SYNC_ENABLED=true`が既に有効化・安定運用されていること**
（`AUTO_CREATE_NEW_RECORDS_ENABLED`単体では取引先マスターリレーション解決は動くが
（`resolve_client_master_relation`/`resolve_zoho_action_client_master_relation`は
`RELATION_SYNC_ENABLED`を個別に見るため）、Round1が未検証のままRound2を有効化すると、
新規作成されるページのリレーションが解決されない状態＝将来的な手戻りが生じやすいため、
実務上はRound1を先に安定稼働させてから進めることを推奨する）。

1. **インフラ整備をデプロイ**: `AUTO_CREATE_NEW_RECORDS_ENABLED`未設定（既定`false`）のまま、
   `src/sync_engine/new_record_builder.py`・`Dispatcher._try_create_new_record()`一式を
   デプロイする。本番挙動は変化しない（従来通り`unknown_record`スキップのまま）。
2. **Slack通知先の確認**: `SLACK_BOT_TOKEN`が設定済みであること、かつ通知を受け取る
   ユーザー（当面は金沢さん、将来的にはマネージャー陣）のdashboard管理画面上の
   `isManager`フラグがONになっていることを確認する（`SLACK_BOT_TOKEN`未設定・
   `isManager=true`のユーザーが0人のいずれの場合も、上記「運用可視性」の通知は
   静かにスキップされ届かない。問題発生に気づきにくくなるため必ず確認すること）。
3. **`AUTO_CREATE_NEW_RECORDS_ENABLED=true`**: Vercel本番環境変数に設定する。有効化後、
   kintone/Zohoで上記「新規作成が実際に機能するdb_key一覧」に該当する新規レコードが
   作成されるたびに、対応するNotionページが自動作成される。
4. **動作確認チェックリスト**（有効化直後、最低1〜2日は毎日確認すること）:
   - Vercelのfunction logsで`/api/webhooks/kintone`・`/api/webhooks/zoho`が引き続き200を
     返していることを確認する。
   - `isManager=true`のユーザー宛Slack DMで✅（成功）・⚠️（必須プロパティ不足）・
     🚨（マッピング登録失敗、または Notion API呼び出し自体の失敗、いずれも最重要）の
     通知内容を確認する。
     - `reason="mapping_registration_failed"`の🚨が来た場合、通知に含まれるNotion page ID
       を直接開き、アーカイブ済みかどうか（「ページはアーカイブ済みです」の文言の有無）を
       確認し、アーカイブされていなければ手動でアーカイブまたは内容を確認して正式な
       マッピングを手動登録する。
     - `reason="notion_creation_status_unknown"`の🚨が来た場合（Notion page IDは通知に
       含まれない）、上記「自動作成されたページを監査ログで確認する方法」のクエリと、
       通知に含まれる`external_id`・`db_key`を突き合わせ、実際にページが作成されたか
       どうかをNotion上で直接確認する（監査ログにも記録が無ければページ自体が作られて
       いない可能性が高い）。ページが見つかった場合は内容を確認したうえで、正式な
       IdMappingを手動登録するか、不要であればアーカイブする。
   - 上記「自動作成されたページを監査ログで確認する方法」のクエリで、想定通りの件数・db_key
     で新規ページが作成されているか確認する（想定外に多い場合、意図しない新規作成が
     頻発している可能性がある）。
   - Notion上で実際に作成されたページを開き、プロパティが正しく入っているか（特に取引先
     マスターリレーションが解決されているか）を目視確認する。
5. **ロールバック**: `AUTO_CREATE_NEW_RECORDS_ENABLED=false`に戻すだけで新規作成は即座に
   無効化できる（コード変更不要）。ただし**既に作成済みのNotionページ（重複・不完全なものを
   含む）はロールバックで自動的には消えない**。フラグを戻した後、上記の監査ログクエリ・
   Slack通知履歴を元に、以下を手動で後始末すること:
   - 重複ページ（同一の取引先/案件等が2件以上作成されている）: 内容を比較し、片方を
     アーカイブする（`RelationReviewQueue`と異なり、重複ページの自動検出・自動マージの
     仕組みは今回未実装）。
   - 不完全ページ（🚨アラートでアーカイブに失敗したまま残っているもの）: Notion上で直接
     内容を確認し、アーカイブまたは正式なIdMappingを手動でDBへ登録する（`IdMappingStore`の
     実装（SQLite/`NotionIdMappingStore`）に応じた方法で行うこと）。

### 2026-08-27の本番障害と対応

**事象**: 2026-08-27 15:13 UTC、kintone Webhook（`POST /api/webhooks/kintone`、
`db_key='client_master'`）が500を返した。原因は`Dispatcher._try_create_new_record()`が
「元レコードが見つからない（`source_target.get_record()`が`None`を返す）」ケースしか
想定しておらず、「取得そのものが例外を送出する」ケース（今回は`KintoneApiError`
「HTTP 400: 不正なリクエストです。」）を考慮していなかったこと。この経路は
`AUTO_CREATE_NEW_RECORDS_ENABLED`を有効化した2026-08-27まで通らなかったため
（未設定時は`unknown_record`として`dispatch()`の早い段階でスキップし、
`_try_create_new_record()`自体が呼ばれない）、それまで露出しなかった。

**対応**: `_try_create_new_record()`内の`source_target.get_record()`呼び出しを
`try/except`で囲み、`ApiError`（`KintoneApiError`/`ZohoApiError`等）・
`requests.exceptions.RequestException`（タイムアウト・接続断等）を捕捉した場合は
例外を伝播させず`DispatchResult(skipped=True, reason="new_record_source_fetch_failed")`
としてスキップへ倒すよう修正した（`raw_record is None`の既存分岐と同じ「取得できなければ
スキップ」という方針に統一）。`logger.exception()`でスタックトレースを残しつつ、上記
「運用可視性（Slack通知）」の`reason="source_record_fetch_failed"`で🚨通知も出す
（Zoho発の新規レコードでも同じ経路のため、kintone/Zoho両方が対象）。

意図的に`ApiError`/`RequestException`以外（`AttributeError`等のプログラミングエラーが
疑われる例外）は握らず、従来通り伝播させる。理由はコード中のコメント
（`src/sync_engine/dispatcher.py`の`_try_create_new_record()`該当箇所）を参照。

あわせて、失敗時にどのレコードで起きたか切り分けられるよう、`KintoneSyncTarget.get_record()`/
`ZohoSyncTarget.get_record()`（`src/sync_engine/sync_targets/`）で、例外を握らず伝播させたまま
`logger.exception()`でapp/module・external_id・db_keyをログへ残すようにした（レコードの中身・
個人情報はログに出さない）。

**未確定の原因調査**（次回同じ障害が起きた場合の切り分け用メモ、コードを読んで分かる範囲。
本番kintone APIへの実アクセスでの検証は未実施）:

- `client_master`の`get_record`経路自体はRound2で初めて使われたわけではない。
  `Dispatcher.dispatch()`本体（Notion以外がソースの場合のコンフリクト判定、`other_tools`の
  現在値取得、`dispatcher.py`内`target.get_record(external_id, db_key=mapping.db_key)`）でも
  2026-08-14のkintone Webhook有効化以降、既知（`IdMapping`が存在する）レコードに対して
  同じ`KintoneSyncTarget.get_record()`が呼ばれている。Round2で新しいのは、**まだ`IdMapping`が
  存在しない・妥当性が未確認のexternal_id**を使って初めて`get_record`を呼ぶ点
  （`_try_create_new_record()`は未知レコードにのみ到達する）。
- `app`（kintoneアプリID）は`KintoneSyncTarget.__init__`で固定され、
  `production_wiring.build_kintone_targets_by_db()`が`KINTONE_APP_ID_CLIENT`環境変数から
  一度だけ読む。`if not app_id or not api_token: continue`のガードがあるため、この環境変数が
  未設定・空文字の場合は`client_master`用のターゲットが`targets_by_db_key`に一切登録されず、
  `_MultiDbKintoneSyncTarget._resolve()`が`None`を返して`get_record`は（例外を出さず）
  静かに`None`を返す。つまり**空文字・None自体は今回の「例外が飛ぶ」事象の説明にはならない**
  （それなら`new_record_source_not_found`スキップになっていたはず）。一方、`app_id`の値
  そのものが正しいかどうか（誤った環境のアプリID、末尾の余分な空白等、コード側では一切
  検証していない）は次回のログ（上記の`KintoneSyncTarget.get_record()`の`app=...`ログ）で
  実際の値を確認できる。
- `HttpKintoneClient.get_record()`は`params={"app": app, "id": record_id}`をそのまま
  `requests`に渡すのみで、`app`/`id`双方とも形式チェック（数値文字列かどうか等）を一切
  行っていない。`record_id`はkintone Webhookペイロードの`record["$id"]["value"]`
  （`kintone_webhook.py`）をそのまま使っており、通常は素の数値文字列のはずだが、
  実際に届いたペイロードでの値そのものは今回のログには残っていない。
- 次回発生時は、追加したログ（`KintoneSyncTarget.get_record()`の`app=/external_id=/db_key=`）
  から、(a) ログの`app`値が実際の`KINTONE_APP_ID_CLIENT`設定値と一致しているか、
  (b) `external_id`が妥当な数値のkintoneレコード番号に見えるか、の2点を確認する
  （いずれもコード・設定の目視確認のみで足り、本番kintone APIを叩く必要はない）。

### 2026-08-27の本番障害対応への追加レビュー対応

上記「2026-08-27の本番障害と対応」で追加した`Dispatcher._try_create_new_record()`の
`try/except`（`ApiError`/`requests.exceptions.RequestException`を握ってスキップへ倒す）に
対するレビュー（shirokuma-sec/kuma-qa）で出た指摘への対応記録。

**クライアント境界での`KeyError`正規化（shirokuma-secレビューWARN対応）**: `_http.py`の
`raise_for_error()`は2xxレスポンスなら何もしないため、「HTTP 200だがボディが期待した形で
ない」場合は`ApiError`でも`RequestException`でもない生の`KeyError`/`IndexError`等が飛び、
上記のexcept節をすり抜けて再びWebhookが500になる同じクラスの穴が残っていた。実際に
起こりうる例として、ZohoのOAuthトークンエンドポイントはリフレッシュトークン失効・
クライアント資格情報不正の際、HTTP 200のままエラーボディ（例:
`{"error": "invalid_client"}`）を返すことが知られている。

対応として、Dispatcher側でcatchする例外の種類を増やすのではなく、`src/sync_engine/clients/`
配下の各クライアントで`raise_for_error()`通過後の辞書アクセス・キャストを`try/except`で
囲み、`ValueError`（JSONデコード失敗含む）/`KeyError`/`IndexError`/`TypeError`/
`AttributeError`を該当ツールの`ApiError`サブクラスへ正規化した（契約を保証する場所を
クライアント層に寄せ、呼び出し元が増えるたびにexcept節を増やさずに済むようにする方針）。
メッセージは`extract_error_message()`（`error`/`message`フィールドのみ最大200文字で拾う
既存の切り詰め方針）を再利用し、トークン・資格情報が例外メッセージに混入しないようにした。
ステータスコードは実際のHTTPステータス（この文脈では2xx）をそのまま使う
（`ApiError.status_code`は他箇所でも「実際に返ってきたステータスを記録する」という
一貫した使われ方をしているため）。

`src/sync_engine/clients/`配下をひととおり確認し、同じ形（`raise_for_error()`直後の
未保護な辞書アクセス・キャスト）の箇所を以下すべてに適用した:

- `zoho_client.py`: `_refresh_access_token()`（`body["access_token"]`、レビューで名指しされた
  箇所）、`insert_record()`（`response.json()["data"][0]`・`result["details"]["id"]`）、
  `update_record()`（同じく`data[0]`アクセス）
- `kintone_client.py`: `get_record()`（`response.json()["record"]`、レビューで名指しされた
  箇所）、`add_record()`（`response.json()["id"]`）
- `notion_client.py`: `create_page()`（`response.json()["id"]`）。`get_page()`/
  `query_all_pages()`/`query_page()`/`get_raw_page()`/`update_page()`/`archive_page()`は
  いずれも`.get()`ベースのアクセスのみ、または生のdictをそのまま返すだけで危険な
  未保護アクセスが無いことを確認済み
- `spreadsheet_client.py`: `append_row()`（`response.json()["updates"]["updatedRange"]`）。
  `_get_header_row()`/`get_row()`/`update_row()`は`.get()`ベースのみで問題無し
- `notion_lookup.py`/`notion_display_resolver.py`は`raise_for_error()`を呼んでおらず対象外

いずれもテスト（`tests/sync_engine/clients/test_*.py`）で「200＋エラーボディ」または
「200＋キー欠落」の場合に生の`KeyError`ではなく正規化された`ApiError`サブクラスになる
ことを確認済み。

**Slack通知テストの追加（kuma-qaレビューINFO対応）**: `_ISSUE_REASON_ACTION_HINTS
["source_record_fetch_failed"]`に対応するテストが無かったため、`mapping_registration_failed`/
`notion_creation_status_unknown`と同じパターンで
`test_notify_new_record_issue_includes_action_hint_for_source_record_fetch_failed`を
`tests/sync_engine/test_slack_notifier.py`へ追加した。

**残存リスク（kuma-qaレビューWARN、対応方針未確定・コード未変更）**: `dispatcher.py`の
`dispatch()`本体（`_try_create_new_record()`とは別の経路）に、今回の修正の対象外の未保護
`get_record()`呼び出しが2箇所残っている:

1. Notion側の現在値取得（`notion_target.get_record(mapping.notion_key)`）
2. コンフリクト判定用の対象ツール（`other_tools`）の現在値取得
   （`target.get_record(external_id, db_key=mapping.db_key)`）

いずれも**既知レコード（`IdMapping`が既に存在する）に対する通常の更新イベント**が通る経路
であり、`_try_create_new_record()`（未知レコードのみが通る新規作成経路）とは別トリガーで
ある。ここで`ApiError`/`RequestException`が飛ぶと、`_try_create_new_record()`と同様に
例外がWebhookハンドラまで伝播し500応答になる（kintone/Zoho発の通常の更新Webhookすべてが
対象になりうるため、影響範囲は新規作成経路より広い）。

**ただし、今回と同じ対処（例外を握ってスキップへ倒す）をそのまま適用してはいけない**。
新規作成パス（`_try_create_new_record()`）では「元レコードが取得できない＝まだ何も
書き込んでいないので何もしない」が安全な帰結だが、この2箇所は**更新パスで比較・書き込み
判断に使う「現在値」の取得**であり、取得に失敗したのを「値が空である」かのように扱って
処理を続けると、`prop.should_sync_to()`の判定や`resolve_conflict()`の入力が欠けたまま
進み、誤った値で他ツールを上書きしてしまう危険がある（本ドキュメント冒頭
「`RELATION_SYNC_ENABLED`が制御する範囲」およびBLOCKER1対応のコメント
（`notion_record is None`分岐、`dispatcher.py`）が警戒しているのと同種のリスク）。
一方、例外を伝播させたままにすると500を返しWebhookイベント自体は失われる（kintone/Zoho側の
リトライに委ねることになるが、リトライされない・リトライ間隔がイベント順序を壊す等の
リスクもある）。どちらが正しいかは業務判断を含むため、今回はコードを変更せず記録のみに
留める。

次に検討する際の論点:

- 取得失敗時に「書き込みを中止して`notify_new_record_issue`相当のSlack通知だけ出し
  イベントは失われた扱いにする」のが妥当か（新規作成経路の`source_record_fetch_failed`と
  対になる`reason`を追加する案）
- それとも、この2箇所だけ`request_with_retry()`のリトライ予算を広げる/呼び出し元で
  リトライするなどして、まず「取得自体が失敗しにくくする」方向で緩和すべきか
- 500のままにして「kintone/Zoho側のWebhook再送に委ねる」現状維持が、実運用上どの程度
  許容できるか（各ツールのWebhook再送保証の有無・間隔を要調査）

**対応不要と判断したもの（記録のみ）**:

- Slack DMの`detail`に例外の`repr`（`f" error={exc!r}"`）を載せている件: 到達範囲がログから
  Slack DMへ広がった点は将来の確認材料だが、現状レコードの中身が含まれる実例は確認されて
  いないため今回は変更しない
- `SyncTarget.get_record()`層（`KintoneSyncTarget`/`ZohoSyncTarget`）と`Dispatcher`層で
  同一例外が2重に`logger.exception`される件: 実害が小さいため許容
- 実クライアントを通した結合テスト・Webhookハンドラのend-to-endテストが無い件:
  レイヤーごとの単体テストで担保されているため今回は追加しない

### 今後の検討候補（2026-08-25、動物チームレビュー記録、対応不要と判断）

「新規レコード作成Slack通知のDM方式切り替え」の動物チーム（shirokuma-sec/obasan-quality/
kuma-qa）レビューで指摘されたが、実害・優先度の観点から今回は対応を見送った項目。将来
関連箇所を触る際の参考として記録しておく:

- **`find_manager_emails()`の委譲を直接検証する単体テストが無い**: `src/incident_detection/
  db.py`から`src/notifications/manager_dm.py`への委譲自体（呼び出しがそのまま転送される
  こと）を確認する単体テストが無い。既存の統合的なテスト（`notify_managers_immediate`経由の
  テスト）で間接的にはカバーされている。
- **DM送信の実処理が2箇所にほぼ同一のまま重複して残っている**: `src/notifications/
  manager_dm.py`の`send_dm()`と`src/incident_detection/notify.py`の`_send_incident_dm()`は
  ほぼ同じ処理（`_resolve_dm_channel()`→`chat.postMessage`）を別々に持つ。既存の単体テストが
  `notify._resolve_dm_channel`/`notify.db.find_manager_emails`をモジュール属性として直接
  monkeypatchしている前提に依存しており、統合するとテストのpatch対象がずれて既存の検証が
  無効化されるリスクがあるため、あえて統合していない（`src/notifications/manager_dm.py`の
  モジュールdocstring「`incident_detection/notify.py`との役割分担について」参照）。
- **`WebhookSlackNotifier`というクラス名が実態と乖離している**: `notify_conflict`は
  Incoming Webhook、`notify_new_record_created`/`notify_new_record_issue`はSlack DMと、
  送信手段がメソッドにより異なるにもかかわらず、クラス名は歴史的経緯によりWebhook前提の
  ままになっている。
- **`src/notifications/manager_dm.py`が遅延importを必要とする循環依存構造になっている**:
  `manager_dm.py` → `src.meeting_sync.slack_approval` → `src.sync_engine.clients.
  notion_lookup` → ... → `src.sync_engine.dispatcher` → `src.sync_engine.slack_notifier`
  という循環importがあるため、`slack_notifier.py`の`_notify_managers()`は`manager_dm`を
  関数内で遅延importしている。将来同種のヘルパーを追加するたびに同じ対策が増殖しうる。
- **`find_manager_emails()`が呼び出しごとに毎回新規DB接続を張る**: コネクションプールが
  無く、`notify_managers()`を呼ぶたびに新しいpsycopg接続を開いて閉じる。呼び出し頻度が
  低い（新規レコード作成・高優先度インシデント検知の発生時のみ）うちは実害が小さい。

「合計5秒のタイムアウト予算」修正（上記ロールアウト手順のDM方式切り替え）に対する検証レビュー
（shirokuma-sec、2026-08-25）で追加指摘された、対応を見送った2点:

- **タイムアウト予算のチェックがマネージャーのループ先頭でしか行われず、厳密な上限保証では
  ない**: `notify_managers()`内の予算チェックは各マネージャーの処理開始前にのみ行われ、
  `send_dm()`内部（最大3回のSlack API呼び出し、各3秒）の途中では働かない。そのため1人目の
  処理だけで理論上最大約9秒（+`find_manager_emails()`のDB接続が最大10秒、これは予算に
  含まれない）かかりうり、ワーストケースの総ブロッキング時間は最大約19秒（当初の
  「N人×最大30秒」からは大幅短縮だが、docstring/ログの「合計5秒」という表現は正確な上限
  保証ではない）。厳密な上限にするには`send_dm()`側にも`deadline`を渡して各API呼び出し前に
  チェックする必要がある。
- **`slack_notifier.py`の`_notify_managers()`内の`manager_dm`遅延importがtry/exceptの外に
  ある**: このモジュールの設計目標（Notifier自体を絶対に失敗させない）を厳密に守るなら
  import文もtryの中に入れるべきだが、`sys.modules`キャッシュにより通常運用で実際に失敗する
  可能性は低い。
