# 隔離容量試験runner

GCP `actionpoint-autocalc` の試験専用DB `crm-capacity-trial-260910` だけを許可する。同じprojectの既定DBや他DBは拒否する。製品のenvやADCを使わず、空HOME・環境変数許可制の子プロセスへ短命SA tokenを標準入力で渡す。秘密を引数・ファイル・ログに出さない。

## 実装した範囲

- project/database/scope/3枠/SA役割を固定。RPCごとにも文書パスを検査する。
- 全RPCに `retry=None` とtimeoutを強制。読取りは55秒、commitは10秒、親が60秒で子を停止する。
- 台帳はprocess間ロックと置換保存。実行前に読取り・書込み・時間を予約し、途中停止でも返却しない。
- 資源作成から14日、累積実行予約24時間、文書読取り300万、書込み50万を制限する。
- 実測費用の証跡登録がない／取得から60分を超えた／累計10 USD以上なら開始を止める。残り10 USDは請求遅延・保存費の予備。
- `smoke` は合成1件の保存・重複・内容競合・実行・再読。`scan` は既存の合成scopeだけを全件観測。
- `scan` は `--role observer` を必須とし、runner指定を認証取得前に拒否する。
- 観測用roleの書込みはRPC前に拒否。これは実IAMのwrite拒否検証ではない。

台帳の予約値は請求操作数ではない。全件queryは100,001件（上限10万件＋超過検出1件）を予約し、実際に返却された文書を別に数える。索引読取り・サービス側再試行・請求遅延は費用の実測で照合する。強制killされたstreamの返却文書数は欠測になりうるが予約は保持する。費用の未測定を0として登録してはならない。

## 起動

リポジトリ直下で `.venv/bin/python -m scripts.capacity_trial --help`。

1. 実資源を作成したUTC epochを `init-ledger --ledger /private/tmp/capacity-trial-ledger.json --created-at EPOCH` へ渡す。
2. 実利用量を確認して `record-cost --ledger ... --usd USD --observed-at EPOCH --evidence 非秘密の証跡参照`。自己申告値の正しさをrunner自体が証明するわけではない。
3. SA impersonationで取得した短命tokenをキーチェーンへ保管する。serviceは `capacity-trial-token-runner` または `capacity-trial-token-observer`、accountは対応する `capacity-trial-{role}@actionpoint-autocalc.iam.gserviceaccount.com`。
4. `smoke --ledger ...` または `scan --ledger ... --scope trial-scan-1103 --role observer`。

JSON成功出力が揃わなければ未完了。終了コード0だけでは判定しない。timeout後はslotを自動回収せず、processとSQL処理の停止を別途確認する。長期保存する証跡は秘密を含まない成功JSON・台帳と、コード版・SDK版を合わせて保管する。

## 未実装・未検証

接続先変更後の実Firestoreへの接続は、このコードのローカルテストでは未検証。実IAM・複合index・永続性・料金をローカルテストで確認済みとは扱わない。

kill/recover（実worker強制終了）、合成大量投入と9走査、監視受信口、バックアップ/復元は未実装。12独立process競合と5操作のcommit保存後応答喪失は下記コマンドを実装したが、実サービスの成否は別途試験記録で確認する。`scan` は投入機能を持たない。1,103＋10,000＋100,000を同時保存すると111,103件になるため、最大10万文書の条件ではそのまま同時投入できない。次段階では削除対象の合成scopeと順番を明示して同時保存数を制御する必要がある。

Neonは `fragrant-silence-24771784` / `br-silent-king-avubki1s` / `ep-shiny-silence-avof39pi` を固定。`neon-probe --ledger ...` は2接続・SELECT・session排他のみ。キーチェーンservice=`capacity-trial-neon-dsn`、account=`neondb_owner`。接続文字列は標準入力で子へ渡し、exact host/DB/role/portを検証し、仮想環境の既存certifi CA bundleで証明書照合を強制する。bundleがなければ停止し、外部のPGSSLROOTCERT等は継承しない。任意SQLプローブを、既存同期の接続ピーク測定やoutbox試験と呼ばない。

旧接続先でNeon Freeだけを使用していた期間の `free-plan-verified` は過去の証跡として保持する。現在のGCP接続先では、初期化直後も移行後もこの根拠を恒久拒否し、`metered` の費用再照合を必要とする。実費0を登録した後でも無料根拠には戻せない。台帳のcreated-atは最初の試験資源作成日時を使う。

失敗JSONの `error_code` は固定分類。`credential_unavailable` は専用キーチェーン認証の取得失敗、`budget_stopped` は費用取得途絶・予算・期間による停止、`trial_timeout` は時間制限、`target_rejected` は許可外接続先。未知SDK例外は `unexpected_error` とし本文を表示しない。子から親へも許可一覧にあるコードだけを渡す。

初回実Neon試験では `sslrootcert=system` の証明書照合が失敗した。TLS検証を緩めず、存在を確認した仮想環境のcertifi CA bundleへ固定する。`verify-full` と `channel_binding=require` は維持する。この修正後の実接続結果は親の試験記録で確認する。


## 旧台帳の接続先移行

既存の試験台帳を作り直さず、`migrate-target --ledger /Users/cnctor/.local/state/crm-capacity-trial-260910/ledger.json` を明示実行する。旧project `cnctor-crm-cap-trial-260910`・旧既定DBだけが移行対象。Firestoreの読取り・書込み・削除予約がすべて0、RPC履歴と返却文書数も0の場合に限る。別接続先・使用済み台帳・移行済み台帳は変更せず拒否する。

元の資源作成日時、Neonを含む累計予約、過去の費用証跡を保持して接続先を変更する。移行直後は費用を再照合待ちにして全実行を止める。通常試験は、移行後の時刻でGCPを含む費用を `record-cost --cost-basis metered ...` により再照合するまで再開しない。請求反映前の小規模試験だけは、下記の別根拠による枠を明示有効化する。旧Neon Freeの根拠で再開できず、14日の期限や累計値もリセットしない。この移行はローカル台帳だけを変更し、DB・認証・IAMを作成しない。


## 競合・応答喪失の段階試験

`concurrency --ledger ... --scope trial-concurrency-1` を、末尾1〜5の各scopeで1回ずつ実行する。1試行につき12件の合成jobを作り、空HOMEの独立した12プロセスが準備完了したことを確認してからstdinの合図で開始する。短命tokenもstdinだけで渡す。各子は1回claimを試みて終了し、枠は解放しない。調整役が終了した子のPID12個・重複しないclaim3個・保存job12件（processing3件／pending9件）と枠の所有者を照合する。5回の成功を1回の結果から推定しない。

`response-loss --ledger ... --scope trial-loss-initialize` は、末尾をinitialize／enqueue／claim／finish／recoverに替えて各1回実行する。接続ガード経由のcommitが成功した直後に、試験専用の応答喪失例外を返す。1回だけcommitされたこと、例外が呼出元まで届くこと、jobと枠の保存状態を再読して確認する。実ネットワーク障害の再現ではなく、保存後に応答が届かなかった場合の挙動試験。recoverの事前claimでは業務workerを起動せず、同期処理終了を確認して停止済みとして扱う。

どちらもrunner専用・未使用scope限定で、途中失敗したscopeを再利用しない。競合の子は45秒以内に準備・終了を待ち、1子あたり50秒を予約する。外側の60秒制限に達したときは調整役と全子を同じプロセスグループごと停止する。失敗時の予約やprocessing枠は自動返却せず、部分結果を成功扱いしない。`claim-child` は競合調整役専用の内部コマンドで、通常環境の直接起動は環境ガードで拒否する。


## 請求反映待ちの小規模推定上限

実測の `metered` と別に、`activate-upper-bound --ledger ... --evidence 非秘密の照合証跡 --confirm-unused-database --confirm-neon-free --confirm-no-extra-resources` を明示実行できる。運用者が専用DB未使用、Neon Freeと残枠、試験対象に追加資源・PITR・バックアップ・TTLがないことを実地照合した証跡が必要。CLIのフラグ自体が外部照合を代行するわけではない。

これは `basis=reserved-upper-bound` の推定上限で、実測費用0への置換ではない。元のcost証跡・期限・累計を保持し、別欄に4 USDを拘束する。固定料金式の3.61571285 USDと予備0.38428715 USDの合計。旧費用がある場合はそれも足して10 USDで停止する。20 USDの総管理予算は変更しない。初回だけ有効化でき、再実行・枠リセットを拒否する。14日を越える保存費は含まず、資源の生涯費用上限や自動撤去を意味しない。

| 上限の要素 | 無料枠を引かない保守計算（USD） |
|---|---:|
| 保存 | 100文書 × 9 MiB（文書1＋index8）×336時間×0.000135616/GiB時 = 0.0400491 |
| index読取り | 1,000 RPC × ceil(40,000 index entries×100文書÷1,000) ×0.033/10万 = 1.32 |
| 文書読取り | 5,000件×0.033/10万 = 0.00165 |
| 書込み | 1,000 RPC×8文書×0.099/10万 = 0.00792 |
| 転送 | 5,000件×2 MiB×0.23/GiB = 2.24609375 |
| 合計 | 3.61571285 |

2 MiBは文書最大1 MiB＋応答の包み1 MiBの保守枠。予備は文書のないRPC応答などにも備える。この料金前提は外部の料金証跡と合わせて判断する。

この方式ではsmoke／concurrency／response-loss／permission-probeと小規模の読取り専用inspect-stateを許可し、scan・Neon新実行は拒否する。全試験で文書名100件（scopeも含む）、RPC1,000回、読取り予約5,000件を共有台帳へ送信前に確保する。失敗・CAS競合も返却しない。既存の総読取り・総書込み・稼働時間上限も引き続き適用する。

文書は更新後の全体4 KiB以下、commitは8文書・要求全体32 KiB以下。部分更新は既存文書をガード経由で追加読取りし、書込みの前提版と一致する場合だけ合成後の全体を検査する。追加読取りもRPC・読取り枠を使う。CAS不一致は製品側の既存の限定再試行へ戻す。全体書込みは新規createだけ。任意フィールド、transform、cursor、offset、collection groupを拒否し、既存確認・状態観測・claimの固定query形だけを許可する。limitなしの全件queryは101件を予約し、100件を超えたら部分結果を破棄する。

`permission-probe --ledger ... --scope trial-permission --role observer` は既存の合成smoke jobを読取り、固定文書 `trial-permission/jobs/synthetic-write-denied` のcreateを1回だけ試みる。通常のobserver書込み拒否は維持し、この試験だけ同じ予算・文書サイズ・文書名予約ガードを通してサーバまで送る。サーバの403 PermissionDeniedだけを成功とし、書けてしまったら試験失敗と台帳の恒久停止を記録する。これは別DBへのアクセス検証ではない。

小規模枠は有効化後に解除しない。metered費用を追加登録してもscan・Neonへの拡張は拒否する。本ファイル末尾の第2段階も小規模限定であり、大規模試験への移行は、元の期限・累計予約を保持する別途のガード変更とレビューが必要。台帳の作り直しで回避しない。


実行は単一の調整役が順次行い、枠有効化と試験を並行起動しない。順番はsmoke → permission-probe → concurrency各回 → response-loss各種。
12子への開始合図は全員の準備完了後に逐次送るもので、同一時刻を保証しない。
失敗scopeの自動やり直しは行わない。結果不明の場合は保存状態を調査してから別途判断する。
小規模枠の実効的な費用制御は文書数・RPC・読取り予約・期間の上限であり、未反映の実費を監視しているわけではない。


`inspect-state --ledger ... --scope trial-concurrency-1 --role observer` は失敗した小規模scopeの保存状態を読取る。
scopeと最大101件のjobsを同じ予算ガードで観測し、jobのID/状態/所有者/枠/試行回数/理由だけ返す。
書込み・回復は行わない。100件超過は失敗し、観測成功を競合試験成功とは扱わない。


このMacの実試験ではgRPCのDNS失敗を確認したため、子・孫の`GRPC_DNS_RESOLVER`を`native`に固定する。
他の値・任意の接続環境変数は継承しない。終了監視はPIDを回収せずgroup停止→回収の順序。
終了済groupのkillpgがEPERMの場合、psで親PID=PGIDのゾンビ存在・非ゾンビ0を確認する。
ps起動/出力の確認に失敗した場合は試験成功を返さない。このためローカル実子テストもpsが許可される環境を要する。
競合子のエラーは全員を待ってから型/固定コードのみ台帳claimant_failuresへ残す。1子でも失敗したら不合格。
今回の実結果・予算残・未完了項目は`docs/sync_capacity_service_trial_results.md`を参照する。


停止確認自体が失敗した場合は、その失敗を優先して返すため元のtimeout分類を失う場合がある。
固定error_codeだけで停止完了や原因を断定しない。競合子の大量stderrは準備待ちを詰まらせる可能性があり、
45秒で失敗する。生stderrはファイルへ保存しない。`observer_write_pending:<UUID>`は送信結果未確認の停止記録であり、自動解除しない。


## 第2段階への明示移行（小規模Firestore限定）

`advance-upper-bound --ledger /Users/cnctor/.local/state/crm-capacity-trial-260910/ledger.json --evidence 非秘密の再照合証跡 --confirm-stage-preflight`。
既存の解除禁止は維持し、このコマンドだけで第1段階から第2段階へ一度移行する。
単一調整役が全試験processの停止を確認してから実行する。旧版processと並行して移行しない。

再照合するもの：同じ専用DB・地域・Standard、料金単価、PITR無効・backup/TTLなし、
Neon Freeと残枠、別経路の文書投入なし、元の期限内、停止記録なし。
フラグは運用者による照合の宣言であり、API検証の代行ではない。根拠欠落時は適用しない。

| 項目 | 第1段階 | 第2段階（すべて開始以来の累計） |
|---|---:|---:|
| RPC予約 | 1,000 | 2,000 |
| 文書read予約 | 5,000 | 10,000 |
| 文書名 | 100 | 100 |
| 拘束USD | 4 | 8（旧4を内包。追加8ではない） |

費用式は旧式と同じ単価・上限で、保存0.0400491、index read2.64、文書read0.0033、
write0.01584、下り4.4921875、合計7.1913766 USD。残り0.8086234 USDを予備とする。
旧段階の全RPC/read/writeが新累計に含まれるため二重に4+8とはしない。実請求額ではない。
総20 USD・10 USD停止条件・元14日期限は維持。scan/Neon/削除/復元はこの段階でも禁止。
文書4KiB・commit8文書32KiB・固定query・100文書名の制限も維持する。

移行前snapshotをupper_bound_transitionへ保存し、旧upper_bound・一般予約・RPC記録・返却数・
created_atを残す。適用後はcreated_at、reserved、rpc_calls、returned_documents、
upper_boundのdocument_names/rpc_reservedが適用前と完全一致し、stage=2/usd=8だけ変わることを照合。
再移行・台帳再作成・累計リセット・使用済scope再試行は許可しない。

競合のclaimant_run_errorsは失敗時点の段階と固定分類。children_stop_verified_at_error=falseは
その時点で停止確認前という意味で、finally後にも稼働中という意味ではない。終了は別途照合する。

## SDKログと失敗診断の分離

内部子は改行を置いてから `CAPACITY_TRIAL_FAILURE_V1 ` に続けて基本4項目をstderrへ出す。
直前のSDKログに末尾改行がなくても識別行を分離する。追加診断は任意の `origin` と `atomic`
だけを許可し、値・キー・型・範囲を厳格に検査する。旧4項目も受理する。
旧受信側は追加項目を拒否するため、実行する親子は同じ版に揃える。
未対応の形式を推測して取り込まない。
親は非0終了した子に限って識別行を解析する。成功stdoutの検査は緩和しない。
外側CLIは従来のJSON形式を維持。任意の例外本文・SDKログは保存しない。

| diagnostic_state | 意味 |
|---|---|
| classified | 許可一覧内の例外型・固定コードを取得 |
| missing | 識別行を取得できなかった。例外がなかったという意味ではない |
| invalid | 診断JSONの形式が不正 |
| unsupported | 型・コードが未対応。任意型名は記録しない |
| ambiguous | 識別行が複数あり、1件を選べない |

これは今後の失敗を調べるための改善。過去の競合4の原因を復元するものではない。
準備前終了は依然として実行段階の記録だけになる場合がある。

発生場所・再試行終了条件と未使用scope6の制約は
[固定診断の設計](../../docs/sync_capacity_failure_diagnostics.md) を参照。
