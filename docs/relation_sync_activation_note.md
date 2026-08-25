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
（`ProductionSyncWiring.id_mapping_store`での逆引き＋`notion_page_client.get_page()`）に
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
