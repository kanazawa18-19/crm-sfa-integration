# 案件管理DB Postgresミラー（ProjectMirror）有効化手順

## 前提

案件管理DB（Notion、約1万件）の全件取得（`NotionDataSource.get_projects()`）が実測約100秒
かかる問題への対応として、Postgres（`ProjectMirror`テーブル）への読み取り専用複製を導入した
（`src/project_mirror/`）。データの正本は引き続きNotionであり、双方向同期パイプライン
（Dispatcher/SyncEvent/IdMappingStore）には一切関与しない。ミラーが古くなっても実害は
「ダッシュボード表示の鮮度低下」のみ。

インフラ（Prismaマイグレーション・Pythonモジュール・Webhookフック・cron登録）は既に
コードとして存在するが、以下2つの環境変数がどちらも未設定（既定`false`）である限り、
本番挙動は一切変わらない。

- `PROJECT_MIRROR_SYNC_ENABLED`: Notion Webhook経由のリアルタイム同期
  （`sync_project_to_mirror`）、および夜間reconciliation cron
  （`GET /api/cron/project-mirror-reconcile`）の両方の**書き込み**を有効化する。
- `PROJECT_MIRROR_READ_ENABLED`: ダッシュボードの**読み取り**元をNotion直読みからミラーへ
  切り替える（`NotionDataSource._fetch_projects()`）。

2つを分けているのは、「書き込み同期は開始したが、ミラーの内容をまだ信頼しきれない」検証
期間を挟み、読み取り元の切り替えだけを独立して後からON/OFF・ロールバックできるようにする
ため（段階導入設計）。**cronの`vercel.json`への登録自体は、`PROJECT_MIRROR_SYNC_ENABLED`が
`false`のままでも書き込みを一切行わない**（`run_project_mirror_reconcile()`が環境変数を
確認し、無効時は`{"skipped": "PROJECT_MIRROR_SYNC_ENABLED is not set"}`を返すのみで
`refresh_all_projects()`を呼ばない）。つまり、cron登録だけでは書き込みは始まらない。

## ロールアウト順序

### 1. インフラ整備をデプロイ

両env var未設定（`false`）のまま、Prismaマイグレーション・`src/project_mirror/`・Webhook
フック・cron登録一式をデプロイする。本番挙動は変化しない（コード上のno-op）。

### 2. 初回バックフィル

`scripts/backfill_project_mirror.py`を実行し、Notion案件管理DB全件を`ProjectMirror`へ
初回投入する（読み取り専用のNotion API呼び出し + 自ミラーへのUPSERTのみで、Notion本番
データを一切変更しない）。

```
set -a; source config/.env; set +a
python scripts/backfill_project_mirror.py
```

`DATABASE_URL`（Neon Postgres接続文字列）・`NOTION_API_KEY`が必要。出力に`synced_count`
（UPSERT件数）・`deleted_count`（今回のバッチで削除されたstale行数、初回は通常0）が
表示される。案件管理DBは約1万件規模のため、`synced_count`が0件、または100件未満のように
極端に少ない場合は成功として扱わず、Notion API権限不足等の失敗を疑うこと
（スクリプト側もこの場合warning表示に切り替える）。

### 3. `PROJECT_MIRROR_SYNC_ENABLED=true`（書き込み同期のみ有効化）

Vercel本番環境変数に`PROJECT_MIRROR_SYNC_ENABLED=true`を設定する。これにより以下2つの
書き込み経路が有効化される（読み取りはまだNotion直読みのまま、`PROJECT_MIRROR_READ_ENABLED`
は次のステップまで`false`のまま維持する）。

- `POST /api/webhooks/notion`経由のリアルタイム同期（案件管理DBのページが更新される
  たびに`ProjectMirror`の該当行を更新、`build_project_mirror_sync_callable`参照）。
- 夜間reconciliation cron（`GET /api/cron/project-mirror-reconcile`、`0 18 * * *` UTC
  ＝JST 3時、`vercel.json`参照）。Webhook配信欠落・ページ削除等の取りこぼしを毎晩
  `refresh_all_projects()`のフル同期で整合させる。

### 4. 数日の検証期間

ミラー（`ProjectMirror.data`）とNotion直読み（`get_projects()`を一時的に強制Notion経路で
叩く、または`PROJECT_MIRROR_READ_ENABLED`を有効化する前のダッシュボードの表示）を突き合わせ、
`notion_page_id`集合の一致・主要フィールドのdiff無しを確認する。

### 5. `PROJECT_MIRROR_READ_ENABLED=true`（読み取り元の切り替え）

Vercel本番環境変数に`PROJECT_MIRROR_READ_ENABLED=true`を設定する。ダッシュボードの
`NotionDataSource.get_projects()`がミラー（`list_projects()`）から読むようになる
（既存の`_cached("projects", ...)`、TTL 600秒はそのまま維持、ミラー読み取りでも無害で
Postgres負荷軽減にもなる）。設定後、実際に全社ダッシュボードをブラウザで開き、表示速度の
改善・データの正しさを目視確認する。

**注意**: このステップの前に手順2（初回バックフィル）を済ませておくこと。バックフィル
未実施のまま`PROJECT_MIRROR_READ_ENABLED`だけ先に有効化すると、`ProjectMirror`が0件のため
エラーにはならず「案件0件」の空ダッシュボードが無言で表示されてしまう（気づけるよう
`_fetch_projects()`側でwarningログは出すが、ダッシュボード上のエラー表示にはならない点に
注意）。

### 6. ロールバック

問題があれば`PROJECT_MIRROR_READ_ENABLED=false`に戻すだけで即座に読み取り元をNotion直読み
へロールバックできる（コード変更不要）。書き込み同期自体（`PROJECT_MIRROR_SYNC_ENABLED`）は
そのまま`true`にしておいても実害は無い（ミラーへの書き込みが継続するだけで、読み取りに
影響しない）。より根本的に無効化したい場合は`PROJECT_MIRROR_SYNC_ENABLED=false`も設定する。

## 多重実行防止（アドバイザリロック）

`refresh_all_projects()`（バックフィルスクリプト・夜間reconciliation cron共通）は、実行
開始時にPostgresアドバイザリロック（`pg_try_advisory_lock`、`src/project_mirror/db.py`の
`try_acquire_refresh_lock`/`release_refresh_lock`）を試みる。夜間cronと手動バックフィルの
実行が偶発的に重なった場合、後から取得を試みた側は`{"skipped": "already_running"}`を返して
即座にスキップする（古い実行が後から完了して新しいデータの`syncedAt`を巻き戻し、mark-and-
sweepで誤って削除してしまう事故を防ぐため）。

## 過去のインシデント: mark-and-sweepの精度不一致による誤削除（2026-08-25）

`upsert_projects_and_sweep()`が基準時刻に素の`datetime.now(timezone.utc)`（マイクロ秒精度）を
使っていたところ、Postgresの`TIMESTAMP(3)`カラムへの保存時に四捨五入（round-half-up、境界に
よっては繰り上がる）でミリ秒精度へ丸められる一方、末尾のDELETEのWHERE比較には丸められて
いない元の値がそのまま使われ、丸め方向次第で`保存値 < 比較用の元の値`が真になり、今まさに
挿入したばかりの行まで誤って削除される事故が本番で発生した（`ProjectMirror`が0件になり、
ダッシュボードの案件管理画面が空表示になる実害。緊急措置として`PROJECT_MIRROR_READ_ENABLED`を
`false`に戻しNotion直読みへフォールバックして復旧した）。`src/relation_sync/db.py`の
`upsert_client_names_and_sweep()`にも同一パターンが存在していた。

対策として、基準時刻の計算を`src/db_utils.py`の`db_truncated_utcnow()`に置き換えた（マイクロ秒を
事前に1000の倍数＝ミリ秒境界へ切り捨てておくことで、Postgres側の丸めが切り捨てでも繰り上げでも
結果が変わらない不動点にする）。

**次に同種のmark-and-sweep方式のミラーテーブル（全件UPSERT→`"syncedAt" < 基準時刻`でDELETEする
パターン）を新設する際は、必ず`src/db_utils.py`の`db_truncated_utcnow()`を使うこと**（素の
`datetime.now(timezone.utc)`を使うと、DBの精度丸めとの不一致で投入直後の行が誤削除される事故が
過去に発生した）。

## 動作確認チェックリスト（各ステップ共通）

1. Vercelのfunction logsで対象エンドポイント（Webhook/cron）の200レスポンスを確認する。
2. `scripts/backfill_project_mirror.py`または夜間cronのレスポンス（`synced_count`/
   `deleted_count`）が想定件数と大きく乖離していないか確認する。
3. Neon Postgres側で`SELECT count(*) FROM "ProjectMirror";`を実行し、Notion側の案件件数と
   概ね一致するか確認する。
