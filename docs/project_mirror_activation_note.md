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

### 前提: advisory lockはpooled接続では無言で機能しない（2026-08-28）

PgBouncerのtransaction pooling（Neonの`-pooler`エンドポイント等）では、advisory lockの
セッション単位の状態が前提として崩れる。ロック取得（`pg_try_advisory_lock`）と解放
（`pg_advisory_unlock`）が別の物理セッションで実行されうるため、**例外は一切出ないまま
排他制御だけが無言で機能しなくなる**。夜間reconcile cronと手動バックフィル
（`scripts/backfill_project_mirror.py`）が偶発的に重なった場合の多重実行防止という
このロックの目的そのものが、静かに無効化されうる——mark-and-sweep方式のこのモジュールに
とっては、過去に実データを全消失させたインシデント（下記）の再発防止の要でもある。

本番の`DATABASE_URL`（Vercel環境変数）が実際にNeonのpooled接続であることを確認したため、
advisory lockを取得・解放する接続だけ**非pooledの`DATABASE_URL_UNPOOLED`**を使うよう変更した
（`src/db_utils.py`の`connect_for_advisory_lock()`。`src/document_generation/approval_db.py`・
`src/project_mirror/db.py`・`src/relation_sync/db.py`の3ファイルが共有する。元は3ファイルに
ほぼ同じ`_connect()`実装がコピーされていたため、ロック専用接続の作成ロジックだけここに
集約した。通常のクエリ用接続はtransaction poolingでも問題なく動くため、引き続き
`DATABASE_URL`のままでよい）。

**この設計は環境変数が正しく設定されていることに依存している点に注意。**
`DATABASE_URL_UNPOOLED`が未設定の場合、例外を出さず`DATABASE_URL`へフォールバックして
動き続ける（＝アプリは正常に見えたまま、多重実行防止だけが無言で無効化された今回と同じ
状態に戻る）。気づけるようwarningログは必ず出す（フォールバック先のホスト名に`-pooler`を
含む場合はさらに強い警告を出す）が、**ログを見ない限り気づけない**。デプロイ後、一度は
本番ログで`DATABASE_URL_UNPOOLED is not set`系の警告が出ていないことを確認すること。

**テストは全てモック（`psycopg.connect`の差し替え）であり、実Postgresでadvisory lockが
実際に排他制御として機能することは未検証**（`tests/test_db_utils.py`の
`connect_for_advisory_lock`系テスト・`tests/project_mirror/test_db.py`の
`test_try_acquire_refresh_lock_prefers_database_url_unpooled`は、いずれも「正しいURLが
`psycopg.connect()`に渡されること」の検証までで、Postgres側の実際のロック挙動は検証して
いない）。確認するなら、非pooled接続（`DATABASE_URL_UNPOOLED`と同じ接続文字列）で2つの
セッションを開き、片方で`SELECT pg_try_advisory_lock(917263540)`を実行してロックを取得した
まま、もう片方の同じクエリが`false`を返すことを見るのが確実（`psql`を2枚起動して手動で
確認できる）。詳細は`docs/quote_approval_note.md`の同種の記録も参照。

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

## 過去のインシデント: 必須プロパティの丸ごと欠落（2026-08-26）

上記の誤削除事故から復旧した翌日、`ProjectMirror`は行数こそ10000件（Notion側の実件数と
一致）だったにもかかわらず、**その全件で「案件名」「営業ステータス」「契約日」等の主要
プロパティが丸ごと欠落し**、「チェーン」「電話番号」等ごく一部のキーしか`data`列に入って
いない状態になっていた。ダッシュボードの集計（`src/api/dashboard_service.py`の
`build_daily_report()`/`build_member_performance()`/`build_manager_alerts()`）はいずれも
`p.get("営業ステータス") is None`の案件を集計対象から除外するため、全件が「ステータス不明」
として除外され、mark-and-sweepの誤削除事故と同じ「ダッシュボードの数字が軒並み0表示になる」
実害が再発した（本番で2回発生。再バックフィルで復旧済み。根本原因は下記「根本原因判明・修正
（2026-08-26）」節参照）。

このとき`scripts/backfill_project_mirror.py`は`完了しました: synced_count=10000
deleted_count=10000`と成功調のメッセージを出力しており、既存の行数ベースのガード
（`_MIN_EXPECTED_SYNCED_COUNT=100`、`refresh_all_projects()`の`_MIN_SYNC_RATIO`）もいずれも
「取得できた行数」しか見ていなかったため通過してしまい、スクリプトの出力からは異常を検知
できなかった。

**教訓: 行数チェックだけでは「行数は正しいが中身が空」という壊れ方を検知できない。**
mark-and-sweep方式のミラーは「何件取れたか」（行数）と「各行の中身が使い物になるか」
（必須プロパティの充足率）は独立した別の壊れ方をしうる。次に同種のミラーテーブルを
新設する際は、行数ベースのガードに加えて、ダッシュボード等の消費側が実際に参照する必須
プロパティ（`is None`/空値チェックでレコードごと除外に使われるプロパティ）が一定割合以上
の行に存在することも別途検証すること。

対策として、`refresh_all_projects()`に必須プロパティ（`PROJECT_SCHEMA`で
`RequirementLevel.REQUIRED`の「案件名」「営業ステータス」）の充足率チェックを追加した
（`src/project_mirror/sync.py`の`_required_property_fill_ratios()`）。取得行数が
`_MIN_ROWS_FOR_COMPLETENESS_CHECK`（20件）以上かつ、いずれかの必須プロパティの充足率が
`_MIN_REQUIRED_PROPERTY_RATIO`（90%）を下回った場合、sweepを中止して既存データを保護し
（戻り値`skipped="insufficient_required_properties"`）、Slack（`_notify_slack_alert()`＋
`User.isManager = true`全員へのSlack DM、`src/notifications/manager_dm.py`）で運用者へ通知
する。`scripts/backfill_project_mirror.py`もこの`skipped`を検知し、「完了しました」という
成功調のメッセージを出さないよう変更した。`_MIN_REQUIRED_PROPERTY_RATIO=90%`は、対象
プロパティがいずれもNotion側でTITLE/REQUIRED区分であり正常データではほぼ全件に値が入って
いるはずという前提のもと、正常な本番データを誤って止めないよう余裕を持たせつつ、今回の
ような壊滅的な欠落（実績0%）は確実に検知できる水準として設定した（実データでの厳密な
検証はできていないため、運用開始後に誤検知が続くようであれば閾値を見直すこと）。

なお`src/relation_sync/sync.py`の`refresh_all_client_names()`（`ClientNameIndex`）は
同じmark-and-sweep方式だが、`ClientNameIndex`は行が`normalizedName`/`rawName`の2列のみで
構成され、`_page_to_index_row()`側でtitle（`raw_name`の元）が空のページはそもそも`rows`に
含めず除外する設計になっている。そのため`ProjectMirror`のような「行としては作られるが
中身の大半のキーが欠落する」壊れ方は構造的に起こりえず、この種の欠落は既存の行数ベースの
`_MIN_SYNC_RATIO`ガード（`rows`の件数減少として現れる）で既に検知できるため、同種の充足率
チェックは追加していない（2026-08-26調査）。

### 根本原因判明・修正（2026-08-26）

本番のreconcile実行時のログに`project_mirror: db_key='project' スキーマに存在しない
未定義プロパティをスキップしました: ['FAX', 'TEL', '【営業部】営業ステータス', '取引先ID',
'取引先名', '住所', ...]`という、明らかに**取引先マスターDB**由来のプロパティ群が出力されて
いたことから根本原因が判明した。

`src/sync_engine/production_wiring.py`の`ProductionSyncWiring.__init__`は、Notion Webhook
プロキシ層（`notion_webhook.handler_with_proxy`）が使う「ページ全体の再取得
（`get_raw_page`）用クライアント」を、`build_notion_clients_by_db()`が返すdb_key単位の
クライアント辞書から`next(iter(notion_clients.values()))`で**任意に1つ選んで**構築していた
（`get_raw_page`/`get_page`/`archive_page`のような単一ページ操作はdb_keyに依存しないため、
「どのDBのクライアントでも良い」という設計意図自体は正しい）。

ところが`src/api/app.py`の`run_project_mirror_reconcile()`（`ProjectMirror`の夜間reconcile）が、
この「db_key不定」のクライアントをそのまま`refresh_all_projects(notion_client=...)`へ渡して
いた。`refresh_all_projects()`は内部で`notion_client.query_all_pages()`を呼ぶが、これは
**そのクライアントに固定されたdatabase_idの全件を返す、db_key依存の操作**である。結果として
「辞書の先頭にたまたま入っていたDB」（実際には取引先マスターDB）の全件を取得し、それを
`ProjectMirror`（案件管理DBのミラー）へ書き込んでしまっていた。取引先マスターDBのページには
「チェーン」「電話番号」等ごく一部だけ`PROJECT_SCHEMA`と偶然キー名が一致するプロパティが
あり、それ以外の「案件名」「営業ステータス」等は存在しないため丸ごと欠落する、という今回の
症状と一致する。`run_relation_sync_reconcile()`（`ClientNameIndex`の夜間reconcile）にも
全く同じパターンで同じ変数が渡されており、こちらは「たまたま辞書の先頭が取引先マスターDB
だった」ため偶然正しく動いていただけで、同じ脆弱性を抱えていた。

**教訓: 「db_key非依存だから」という理由で辞書から任意に選んだ共有クライアントを、
db_key依存の操作（`query_all_pages()`等）を呼ぶ箇所へそのまま渡してはいけない。**
`get_raw_page`のような単一ページ操作と`query_all_pages()`のような全件取得操作は、同じ
`HttpNotionClient`という型を持ちながら、前者はdb_key非依存・後者はdb_key依存という非対称な
性質を持つ。この非対称性が変数名からもコード上の型からも読み取れなかったことが、事故が
半年近く（Webhook同期は2026-08-17開始、reconcileは同日以降に配線）気づかれなかった一因。
次に同種のミラー/インデックステーブルを実装する人は、①db_key依存の操作を呼ぶ箇所には必ず
明示的にそのDB専用のクライアントを構築・注入すること（辞書から任意に選んだクライアントの
使い回しは単一ページ操作に限定する）、②可能であれば「任意のDBが入りうる変数」と「特定の
DBでなければならない変数」を名前で区別すること、を徹底すること。

対策として、`ProductionSyncWiring`の該当属性を`notion_page_client`から`any_db_page_client`
へ改名し（「どのDBかは不定」であることを名前から分かるようにする）、案件管理DB専用の
`project_mirror_notion_client`・取引先マスターDB専用の`client_master_notion_client`を
新設した。`run_project_mirror_reconcile()`/`run_relation_sync_reconcile()`はそれぞれ専用
クライアントを使うよう修正し、`any_db_page_client`をdb_key依存の操作へ渡すことを禁止する
旨を`ProductionSyncWiring`のdocstringに明記した。回帰テスト
（`tests/api/test_app_webhooks_and_cron.py`の`test_cron_project_mirror_reconcile_runs_refresh_when_secret_matches`/
`test_cron_relation_sync_reconcile_runs_refresh_when_secret_matches`）では、`any_db_page_client`と
専用クライアントに別々のフェイクオブジェクトを設定し、実際に渡されたのが専用クライアントの
方であることをオブジェクトのアイデンティティ（`is`）で検証している。

## 動作確認チェックリスト（各ステップ共通）

1. Vercelのfunction logsで対象エンドポイント（Webhook/cron）の200レスポンスを確認する。
2. `scripts/backfill_project_mirror.py`または夜間cronのレスポンス（`synced_count`/
   `deleted_count`）が想定件数と大きく乖離していないか確認する。
3. Neon Postgres側で`SELECT count(*) FROM "ProjectMirror";`を実行し、Notion側の案件件数と
   概ね一致するか確認する。
