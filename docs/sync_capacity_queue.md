# 同期APIの接続前の実行枠・要求保管

## 目的と配備状態

同期4Webhookとスプレッドシート行作成の再処理入口で、**DB接続・配線組立より前**に要求を永続保存し、全配備で共通の枠を取ったworkerだけが処理する。既定は無効。コード準備のみで、本番資源の作成・配備・DB接続・cron有効化は実施していない。

```text
Webhook → 認証 → Firestore保存 → 保存確認後200（受理）
                      ↓
Scheduler → worker → job＋共有枠を同時確保 → DB/同期処理
                      ↓                       ↓
                  枠満杯なら保持         結果保存＋枠解放
```

200は同期先への反映完了ではない。`job_id` と `state` を返す。過負荷でも保管先が正常ならpendingとして受理する。APIリクエスト内のbackground taskに処理を委ねない。

## 対象と容量の境界

| 対象 | 有効時の動作 |
|---|---|
| POST `/api/webhooks/notion` | HMAC署名確認 → 保管 |
| POST `/api/webhooks/kintone` | URL secret確認 → 保管 |
| POST `/api/webhooks/zoho` | body token確認 → 保管 |
| POST `/api/webhooks/spreadsheet` | ヘッダーsecret確認 → 保管 |
| GET `/api/cron/spreadsheet-outbox-drain` | 既存cron認証 → 保管 |
| GET `/api/cron/sync-capacity-drain` | 既存cron認証 → 1件処理 |

その他のcron・ダッシュボード・診断・文書作成・直接CLI・別アプリは対象外で、**別の接続予算が必要**。この仕組みだけではDB全体の上限を保証しない。`docs/sync_api_connection_capacity.md` の接続本数照合と合わせ、全体の接続上限から対象外・管理用接続・常駐接続を差し引き、1処理で同時に使う接続本数で割って枠数を決める。安全な既定枠数は設定しない。

プロセス内はworker lockで直列化。Dispatcherは各呼出しの戻り値を観測し、共有singletonの `last_result` を読まず、Zohoの複数レコードの途中で起きた部分失敗も累積する。既存受信診断用の記録は枠内で更新するため、その時刻は受付時刻ではなくworker処理開始時刻となる。

## 設定と切替条件

`config/sync_capacity.example.env` を参照。すべての配備で以下を一致させる。

- `SYNC_CAPACITY_ENABLED`: 未設定または `false` の場合は旧経路。`true` で有効。それ以外は対象APIを503にする。
- `SYNC_CAPACITY_FIRESTORE_PROJECT` / `SYNC_CAPACITY_FIRESTORE_DATABASE` / `SYNC_CAPACITY_SCOPE`: 同じDBを使う全配備に同じ値。scopeを配備別に分けると容量保証が崩れる。
- `SYNC_CAPACITY_SLOTS`: 明示必須、1〜50。共有scope文書のlimit・固定slot集合と不一致なら拒否する。初期化済みscopeの枠数を起動時に上書きしない。
- Google認証はNeonの接続認証とは別に用意する実行サービスアカウントのADC。読書き権限を必要なFirestoreへ限定し、公開クライアントからの書込みは禁止する。FirestoreのIAMを破れる利用者への耐性は対象外。

有効時に設定・認証設定・保存・読取りが失敗しても旧DB経路へ戻さない。401/400/413/409/503を返す。署名省略用 `ALLOW_UNSIGNED_WEBHOOKS` は有効なキュー入口では認めない。Notion購読の初回verification_token交換はキュー経路では受け付けないため、購読・鍵設定を済ませてから有効化する。

`FIRESTORE_EMULATOR_HOST` はローカルのdemo-*プロジェクトかつlocalhost/127.0.0.1だけ。`VERCEL` または `K_SERVICE` のある配備環境では拒否する。

切替は、対象入口に旧経路を残した配備が無いこと、旧処理が停止したことを確認して行う（最長実行時間の経過とDB処理終了を照合）。**新旧混在中は旧経路が共通枠を取らない。** ロールバックでフラグをfalseへ戻す場合も、queue worker停止・実行中ジョブの終了確認・保管済み要求の扱いを決める必要がある。

## 保存と重複排除

Firestore `sync_capacity_scopes/{scope}` にversion、limit、固定slotsのmapを保管し、`jobs/{hash}` に要求・状態・owner・試行数・時刻を保管する。jobとslotは、読み取った更新時刻（update_time）が変わっていないことを条件に、同じ原子的commitで更新する。新規jobには「まだ存在しない」条件を付ける。受付ではscopeのversionを同じ値で更新する操作にもupdate_time条件を付け、設定の照合から保存までの変更を検出する。どちらかの条件が外れたら両方とも更新しない。

読取り中にサーバーのロックを保持せず、保存時の比較で競合を検出する方式（CAS）を採用した。読取りのみの重複応答・満枠応答・dry-runは書き込まない。SDKのcommitは `retry=None, timeout=10` とし、結果不明の自動再送を止める。

- 最大bodyは256KiB。署名確認は元bodyに対して行う。保存ヘッダーはループ識別の `X-Sync-System-ID` だけ。認証ヘッダー・URL query・bodyの接続用token/verification_token/secret/authorizationは保存しない。
- Notion/Kintoneの通知ID、または認証情報除去後のbody内容hashと送信元を使う。レコードIDは更新ごとに同じなので重複排除に使わない。同一通知IDで別内容は409で明示拒否し、既存要求を上書きしない。
- Notionの配送試行回数 `attempt_number` だけは内容照合から除外する。業務データが同じ正規再送を409にしない。Notionの通知ID・時刻が両方無い入力は受付UUIDで別jobとし、同じ内容の将来の編集を永久に重複扱いしない。通常のGAS通知は毎回の `editedAt`、Kintoneは更新日時を含む。
- Zohoのserver_time欠落は受付時刻で一度だけ補完し、そのjob内では固定。同じ内容の後日の別編集を捨てないため受付UUIDで別jobにする。この例外では配信の重複を排除しない。
- outbox定期呼出しは受付UUIDを持ち、同一bodyでも新規jobとする。毎回処理が必要であり、最初のcompletedで将来の全定期実行を重複扱いにしない。
- jobとslotにはTTLも期限削除も設定しない。容量と保存料金は継続監視が必要。本文に業務情報を含むため、運用者CLIのinspectは本文を表示しない。

## 状態と停止時の保全

| 状態 | 意味・次の処理 |
|---|---|
| pending | 保存済み。枠が空けば次のdrainが取得 |
| retry | 業務処理を呼ぶ前の配線初期化失敗。60秒後から再取得可能 |
| processing | jobと枠を同時に所有。期限による取り直しはしない |
| completed | 呼出しが終了し、観測した結果に要確認事項が無い |
| needs_attention | 副作用の可能性や部分失敗がある。自動再送しない |

workerは業務処理を呼ぶ前の初期化例外だけretryに戻す。業務処理中の例外・HTTP500・不明skip・`new_record_*`・一部書込み先の未反映・Notion副フック失敗はneeds_attentionに保持する。正常に呼出しが終了した後は枠を解放できる。永続結果保存の応答が失われた場合、追加のrelease操作はしない。

claimの応答喪失・プロセス強制終了では処理の有無が確定しないため、jobと枠を保持する。実行枠にTTLを付けると、旧workerが生きている間に別workerへ再割当され上限を超えるため採用しない。通信断・Firestore transaction競合時もDBへ迂回しない。

## Notionの差分・時刻の既存制約

Notionはworker実行時に最新ページを取得する一方、通知の `updated_properties` だけを同期する。待機中のA項目更新・B項目更新を後で処理すると双方のページ時刻が同じになり、後の通知が既存watermarkでstaleになる。この**Notion staleはcompletedにせずneeds_attentionへ保全**する。項目別の手動照合が必要。

全項目snapshotへ変更すると、Notionに残った古い別項目の値で他ツールを上書きする既存の危険があるため差分指定を維持した。項目別watermarkの導入は別イシュー。本対応は受理要求の保管と実行数制限であり、全イベントの自動修復・全同期先への完全反映を保証しない。

## 手動復旧

以下は操作方法であり、本番では未実行。通常のローカルCLIから、対象Firestoreへ権限のある運用者だけが実行する。

```bash
python -m src.sync_capacity initialize
python -m src.sync_capacity inspect --state processing
python -m src.sync_capacity recover --job-id JOB_ID --owner OWNER
```

上記は読取り/dry-run。初期化を承認して実施するときだけ `initialize --apply`。既存scopeは上書きしない。

`python -m src.sync_capacity drain` は別の実行コマンドで、dry-runではない。`SYNC_CAPACITY_ENABLED=true` とFirestore設定・認証が必要で、共通枠を取得して最大1件のDB・同期先への実書込みを行う。運用者端末も同じscopeを使い、本番向け実行は配備・試運転の指示を受けてから行う。

processingの回収は、**全対象worker入口・定期呼出しを停止し、全配備の実行終了とDB処理終了を外から確認してから**、同じJOB_ID・OWNERに `--apply --confirm-worker-stopped` を加える。ownerがjob/slotの双方と一致しない操作は拒否。適用後はneeds_attentionに移し枠を解放する。自動でpendingに戻さない。

needs_attentionは実際の同期先とwatermark/outboxを照合し、不足分だけを既存の修復手順で直す。本文の全面再送は副作用やwatermarkによる欠落があるため復旧コマンドに含めていない。停止を確認できない場合は枠を回収せず保留する。ownerはランダムUUIDで、revision/instanceとの対応は保存していないため、ownerだけで個別workerの停止を証明できない。構造を固定した処理ログにはjob_id・owner・slot・状態だけを出し、本文や秘密は含めない。claim応答不明時はownerを出して保管文書と照合する。このログは全配備の停止確認を置き換えない。

## 定期drainと監視の配備要件

2026-09-10の実サービス読み取り結果は[容量・流量・監視の確認](sync_capacity_live_readiness.md)を参照。
対象GCPのFirestore DBは0件、同期drain未登録、既存監視6件は検証用で全停止。
本番容量保証は未充足。Firestore APIは一覧閲覧時に自動有効化され、監査ログで確認済み。

- Firestoreのdatabase、保管時暗号化、IAMアクセス範囲、バックアップ/復元方法を配備前に確認する。pending/processingを含む全job/slotのTTLは禁止。
- `config/firestore.sync-capacity.indexes.json` の複合indexを配備し準備完了を確認する。query失敗でも保存済みpendingは消えない。
- Cloud Scheduler等から既存cron認証付きで `/api/cron/sync-capacity-drain` を定期呼出しする。本変更ではcronを新規有効化しない。
- 現状は **1call=最大1job**。毎分1callなら理論上限は1日1,440件であり、実際には実行時間・競合・枠不足により下がる。流入量は未実測。必要頻度・並列呼出し数・最大実行時間を配備前に照合する。Vercelの日次cronだけでは十分でない。
- 必要条件は **実際の処理可能件数 > Webhookの受理件数 + outbox定期要求数 + 初期化再試行件数**。現設定のoutboxは日次1回だが、頻度変更後はその数も加える。単なるHTTP呼出し成功数には満枠・ローカルbusyも含まれるため、処理可能件数の代用にしない。最古要求の待ち時間が増え続けないことと、積み残しを減らせる余力を測定する。
- workerが停止・スリープしてもpendingは保管されるが進まない。最古pending/retryの経過時間、processing保持数、needs_attention件数、最終completed時刻、Scheduler登録・成功呼出し、処理所要時間を監視対象にする。自動監視の実装・実Bot照合は別の未完了事項。
- 初回claimを0〜0.5秒ずらし、確実な比較競合（ABORTED / ALREADY_EXISTS / FAILED_PRECONDITION）だけ、最大8回・再試行開始予算3秒で読み直す。通信結果不明を含む例外連鎖は再試行しない。3秒は処理全体の上限ではなく、最後のcommit待ちは最大10秒、読取りにもSDK所定の待ち時間がある。競合や障害で失敗してもDBへ迂回せず保管を維持する。
- 実行時間超過で保持枠が増えると処理能力が下がる。実環境の最大所要時間をプラットフォーム上限と照合し、保持枠の増加を通知する監視と、全worker・DB処理停止の確認から復旧までの所要時間を配備前に検証する。時刻だけで枠を自動解放しない。初期化retryには回数上限が無いため、その滞留と処理量消費も監視する。

保存前にFirestore自体が落ちた要求は200で受理しないため、送信元の再送に依存する。`gas/onEdit.js` はfetch1回・エラーをログに出すだけで再試行が無いため、この未受理領域まで無損失は保証しない。Zoho Notificationsの再送保証も未確認。満枠要求はFirestoreへ保存して200を返すので、この送信元制約を過負荷時に発生させない。

## 検証の範囲

単体テストは認証前保存0、DB配線前受付、保存失敗503、秘密除去、時刻補完、処理前/処理中の失敗分類、応答喪失、強制終了、部分失敗、Notion副フック/差分保全を確認する。

`tests/sync_capacity/test_firestore_emulator.py` は公式Firestore emulatorが設定された場合のみ実行し、独立storeの共有枠、競合claim、所有者照合、期限再割当なし、手動回収、設定不一致、重複衝突を検証する。未設定なら実Firestoreへ接続せずskipする。**emulator通過はFirestore実サービス・IAM・本番index・Vercel/Cloud Runの実Bot動作を検証した意味ではない。**

## 参照した公式仕様

- [Firestore Commit](https://docs.cloud.google.com/firestore/docs/reference/rest/v1/projects.databases.documents/commit)：共有枠と要求の書込みを原子的に一括確定する根拠。外部の同期処理は再試行される保存関数に入れない。
- [Firestore emulatorの接続](https://firebase.google.com/docs/emulator-suite/connect_firestore)：実サービスへ接続せずにSDK経由で検証する。実サービスのIAM・複合index・永続性の証明には使わない。
- [Notionのイベント配信](https://developers.notion.com/reference/webhooks-events-delivery)：未受理の場合の再送には回数制限があるため、保管前の障害まで無損失とはしない。

2026-09-09〜10に確認。gRPC例外の変換・SDKの再試行・WriteBatchの前提条件とcommit呼出しについては、検証環境に導入したSDKの実装も照合した。

## 旧版3483e8fの独立検証（2026-09-09 主機CX）

| 検証 | 実測 |
|---|---|
| 全pytest | 2,883成功、emulator用6件skip、既存Starlette警告1件 |
| 対象の単体＋公式emulator | 41成功（上記と重複する35単体＋skipした6件） |
| 満枠中の保管→逐次処理 | 40件保存、40件completed |
| 偽owner・設定不一致・停止確認なし復旧 | 拒否 |
| 子プロセス強制終了 | processingと枠を保持 |
| claim/finishの実commit直後の応答喪失 | 要求を保持、曖昧な再実行・追加解放なし |
| 最終12プロセス×5回、3枠 | 取得12、競合例外48、上限超過0 |

Firestore SDK 2.30.0、公式emulator 1.22.0、ローカル専用の架空プロジェクトで検証した。実Postgres接続ピーク・Firestore実サービスの永続性・IAM・本番の処理量は未検証。検証サーバーは停止し待受終了を確認した。

旧版では競合時の処理開始失敗が未解消だった。初版は同条件60回で取得8・空/満枠9・例外43、途中版は取得11・空/満枠27・例外22だったが、3483e8fは上表となった。例外の連鎖は`ValueError → Aborted(409) → grpc ABORTED`で、確実な競合として識別されていた。改善を断言せず、翌日の比較試験・保存方式変更へ引き継いだ。

この時点のSEC・品質レビューはBLOCKER 0 / WARN 0。外部レビューは自動承認レビューの拒否で未実施だったが、9月10日の本人承認後に両社への送信・回答取得を完了した。採否・変更理由・比較試験は [外部レビュー採否](sync_capacity_external_review.md) を参照。本番配備は引き続き保留。

証跡：`/private/tmp/crm-capacity-qa/pytest_full_ultimate.log`、`result_ultimate.log`、`contention_ultimate.log`。これらの一時ファイルが消えても再開条件を失わないよう、結論と件数は本節に記録した。

## 修正後の独立検証（2026-09-10 主機CX）

| 検証 | 実測 |
|---|---|
| 全pytest | 2,896成功、emulator用10件skip、既存Starlette警告1件、29.21秒 |
| 対象単体＋公式emulator | 58成功（上記と重複する48単体＋skipした10件）、3.28秒 |
| 12独立プロセス×5回・3枠 | 取得15、満枠45、例外0、上限超過0、最大0.501秒 |
| 受付・完了・取得を混在させた12プロセス×3回 | 受付18、完了9、取得8、満枠1、例外0。保存件数・枠とjobの所有者一致、最大0.412秒 |
| 満枠時の保管→逐次処理 | 40件保管→40件completed |
| 強制終了・停止確認後の手動復旧 | job/枠保持、dry-run不変、停止確認なし拒否、適用後needs_attention |
| initialize/enqueue/claim/finish/recoverのcommit成功後に応答喪失を注入 | 全5操作でcommitは各1回のみ。実保存状態を照合。claimでは業務処理未開始・再取得なし |
| scopeだけ／jobだけが先行更新された比較失敗 | 2ケースとも両文書への変更を拒否 |

外部2社の回答取得・採否整理を完了し、実装者とは別のSEC・品質・QAでBLOCKER 0 / WARN 0。品質INFOの内部引数名整理は動作に影響しないため見送った。修正後の他社への再送は行っていない。**コード準備としての実装・レビュー・隔離検証は完了。本番の容量保証は未完了で、配備は保留。** 実Bot照合・自動監視・実Firestore/IAM・実Postgres接続ピーク・流量・実行時間は上記配備条件として残る。

証跡は `/private/tmp/crm-capacity-qa/` の `cas_product_full_pytest.log`、`cas_product_target_pytest.log`、`cas_product_results.jsonl`、`cas_product_mixed.jsonl`、`cas_product_safety.log`、`cas_product_unknown.log`。SDK 2.30.0 / 公式emulator 1.22.0の隔離環境で実施し、検証サーバー停止・8787番待受なしを確認した。これらの測定を本番性能とは扱わない。
