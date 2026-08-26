## 概要

Gmail連携（`src/gmail_sync/`）は2つの同期経路を持つ:
- `sync_all()` / `sync_rep()`: `GET /api/cron/gmail-sync`（Vercel Cron、1日1回）から呼ばれる、
  直近2日分をスキャンするフル同期。Push未登録の担当者への安全網、および見逃し防止の日次
  セーフティネット。
- `sync_rep_incremental()`: `gmail_push_webhook.py`（Cloud Pub/Sub Push通知）から呼ばれる、
  保存済み`historyId`起点の増分同期（主経路、ほぼリアルタイム）。

Push通知のwatch登録・延長は`src/gmail_sync/watch_registration.py`（`GET /api/cron/gmail-watch-renewal`、
1日1回、`RepGmailConnection.historyId`/`watchExpiration`をrepごとに管理）が担う。

## 過去のインシデント1: TOKEN_ENCRYPTION_KEY不一致によるトークン復号失敗（〜2026-08-25発見）

2026-08-15にGmail連携したものの、以降9日間`EmailLog`が0件のまま（同期が一度も成功していない）
という状態が本番で発生していた。原因は`decrypt_token()`が`cryptography.exceptions.InvalidTag`で
失敗し続けていたこと（過去の`TOKEN_ENCRYPTION_KEY`不整合インシデント（2026-08-16〜08-18、
`src/api/token_encryption_healthcheck.py`のdocstring参照）の影響で、当時暗号化されたリフレッシュ
トークンが現在の鍵では復号できなくなっていたと推測される）。`token_encryption_healthcheck`
（バックエンド自身のencrypt→decryptラウンドトリップ）は正常を返し続けていたため、この不一致
（暗号化した側と復号する側の鍵が食い違うケース）は検知できなかった。

**対応**: 担当者（金沢さん）にダッシュボードの「設定」→「Gmail連携」から連携解除→再接続して
もらい、現在の鍵で暗号化された新しいリフレッシュトークンに置き換えることで復旧した。

**教訓**: `token_encryption_healthcheck`は「鍵が今壊れていないか」は検知できるが、「過去に別の
鍵で暗号化されたデータが今の鍵と一致するか」は検知できない。同種の暗号化トークンを扱う機能
（Gmail連携・Drive連携（見積書承認フロー）といった個人OAuth接続）で、接続後まったく同期実績が
無い（`lastSyncedAt`がnull）ケースを見つけたら、まずこの鍵不一致を疑うこと。

## 過去のインシデント2: 削除済みメッセージ(HTTP 404)によるhistoryIdカーソル固着（2026-08-25〜08-26発見）

インシデント1の復旧直後、Push通知経由の`sync_rep_incremental()`が2026-08-25T10:07〜08-26T01:12の
15時間で170回連続して同じエラー（`GmailApiError: HTTP 404 Requested entity was not found`、
Gmail側で既に削除済みのメッセージを`messages.get`で取得しようとして失敗）を出し続けていることが
発覚した。

原因は、`sync_rep_incremental()`のループ内で1メッセージの処理失敗（例外）がループの外まで伝播し、
ループ完了後にのみ呼ばれる`db.update_history_id()`に到達できなくなること。`historyId`カーソルが
前進しないため、次のPush通知のたびに同じ`startHistoryId`から`list_history()`をやり直し、同じ
削除済みメッセージに再度ぶつかって同じ失敗を繰り返す無限ループになっていた。この間、新しく
届いた本来同期すべきメールも一切処理されない（カーソルがそこで固まっているため）。

**対応**: `_process_message_ref_or_skip()`（`src/gmail_sync/sync.py`）を新設し、`GmailApiError`の
うち`status_code == 404`（Gmail APIの`request_with_retry()`が429/5xxを既にリトライ済みのため、
ここに到達するのは「リトライしても直らない」エラーのみ）の場合だけスキップして処理を継続、
それ以外の例外（一時障害の可能性がある）は握りつぶさず伝播させ、`historyId`更新も従来通り
止める、という切り分けにした。`sync_rep_incremental()`・`sync_rep()`双方のループをこの
ラッパー経由に統一した。

**次に同種の「historyId/カーソルベースの増分同期」を新設する際の教訓**:
- カーソル前進（`update_history_id()`相当）をループの外・末尾でまとめて行う設計では、
  ループ内の1件の失敗がカーソル前進そのものを止めてしまい、恒久的に取得不能な1件のせいで
  以降の全件が見えなくなる「詰まり」が起きうる。恒久的に取り除けないエラー（404等、
  リトライ層を通過してきたもの）は個別にスキップして継続する設計にすること。
- 一方で、一時障害（ネットワークエラー等）まで安易にスキップしてカーソルを進めてしまうと、
  今度は「本来リトライすれば拾えたはずのメッセージ」を恒久的に見逃す事故になる。「恒久的に
  ダメ（404等）」と「一時的にダメかもしれない」を明確に区別してから対応を分けること。
- 未対応の残課題（次にこのコードへ触れる際の参考、obasan-qualityレビュー2026-08-26）:
  - `_process_message_ref()`（生）と`_process_message_ref_or_skip()`（404吸収ラッパー）が
    似た名前・シグネチャで並存しており、将来の呼び出し元が誤って生の方を直接使うと今回の
    バグが再発しうる。次にこのモジュールへ大きく手を入れる際は、「安全な方をデフォルト名に
    する」（生の方に`_impl`等の目立つ接尾辞を付ける）設計への寄せ替えを検討すること。
  - 404 catchのスコープが`_process_message_ref()`関数全体（Gmail `get_message()`呼び出し
    以外の、連絡先照合・EmailLog記録・Notion更新等も含む）になっている。現状`GmailApiError`を
    送出しうるのは`get_message()`呼び出しのみだが、将来同関数内に別のGmail API呼び出しを
    追加する場合は、404の扱いをその呼び出しごとに個別に検討すること（メッセージ本体の削除と
    無関係な404を誤って同一視しないため）。

## 過去のインシデント3: tz-naive/tz-awareなdatetime混在によるwatch延長cronのクラッシュ（2026-08-25〜08-26発見）

インシデント1の復旧作業（担当者が手動でGmail連携を再接続）で`watchExpiration`に初めて値が
入った直後から、`GET /api/cron/gmail-watch-renewal`（`renew_all_watches()`、Push通知watchの
自動延長、Vercel Cronで1日1回）が毎回以下のエラーでクラッシュするようになった。

```
TypeError: can't subtract offset-naive and offset-aware datetimes
```

原因は`_needs_renewal()`（`src/gmail_sync/watch_registration.py`）で、`conn.watch_expiration`
（psycopg経由、`RepGmailConnection.watchExpiration`＝`TIMESTAMP(3)`列由来のtz-naiveなdatetime）
と`now`（`datetime.now(timezone.utc)`、tz-aware）をそのまま引き算していたこと。
`watch_expiration`が`None`の間（＝初回登録前）は`_needs_renewal()`の`is None`早期returnで
このコードパスを通らないため顕在化せず、**一度でもwatchが登録されて値が入った後に初めて発火する**
遅発性のバグだった。この間、失効が近づいても延長が一切行われないため、Google仕様上の上限
（登録・延長時点から最大7日）に達するとPush通知経由のリアルタイム同期が無音で停止するリスクが
あった（日次のフル同期セーフティネットは残るため、データが完全に失われるわけではない）。

同種の問題は既に`src/email_reminders/reminder_check.py`の`_elapsed_hours()`で発生・対処済み
だったが（`sent_at`のtz-naive/aware変換）、`watch_registration.py`側は横展開されていなかった。

**対応**: `src/db_utils.py`に共通ヘルパー`ensure_utc(dt)`を追加し（`db_truncated_utcnow()`と
同じ「DBのタイムスタンプ精度・タイムゾーン問題への対処」という置き場）、`_needs_renewal()`で
`conn.watch_expiration`をこの関数に通してから`now`と演算するよう修正した。回帰テストは、
DBが実際に返す形（tz-naive）の`watch_expiration`を渡すケースを追加した（既存テストは
tz-awareな値のみを渡していたため、このバグを検出できなかった）。

**次に同種のコードを書く人への教訓**:
- psycopg経由で読んだdatetime（`TIMESTAMP(3)`等、タイムゾーン情報を持たないPostgresカラム由来）
  は、Pythonの`datetime.now(timezone.utc)`のようなtz-awareな値と直接演算・比較する前に、必ず
  `src/db_utils.ensure_utc()`（またはそれと同じ考え方の変換）を通してtz-awareへ揃えること。
- このクラスのバグは、対象のカラムが`NULL`の間は早期returnやNoneチェックで問題のコードパスに
  到達せず「動いているように見え」、実際に値が入って初めて`TypeError`として顕在化する遅発性を
  持つ。ローカル・テスト環境で対象カラムが常に`NULL`のまま検証していると気づけないため、
  新しいDB由来のdatetime比較を書く際は、値がある場合のケースを明示的にテストすること。
