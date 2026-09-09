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

12独立process競合、5操作のcommit応答喪失、kill/recover、合成大量投入と9走査、監視受信口、バックアップ/復元は未実装。`scan` は投入機能を持たない。1,103＋10,000＋100,000を同時保存すると111,103件になるため、最大10万文書の条件ではそのまま同時投入できない。次段階では削除対象の合成scopeと順番を明示して同時保存数を制御する必要がある。

Neonは `fragrant-silence-24771784` / `br-silent-king-avubki1s` / `ep-shiny-silence-avof39pi` を固定。`neon-probe --ledger ...` は2接続・SELECT・session排他のみ。キーチェーンservice=`capacity-trial-neon-dsn`、account=`neondb_owner`。接続文字列は標準入力で子へ渡し、exact host/DB/role/portを検証し、仮想環境の既存certifi CA bundleで証明書照合を強制する。bundleがなければ停止し、外部のPGSSLROOTCERT等は継承しない。任意SQLプローブを、既存同期の接続ピーク測定やoutbox試験と呼ばない。

旧接続先でNeon Freeだけを使用していた期間の `free-plan-verified` は過去の証跡として保持する。現在のGCP接続先では、初期化直後も移行後もこの根拠を恒久拒否し、`metered` の費用再照合を必要とする。実費0を登録した後でも無料根拠には戻せない。台帳のcreated-atは最初の試験資源作成日時を使う。

失敗JSONの `error_code` は固定分類。`credential_unavailable` は専用キーチェーン認証の取得失敗、`budget_stopped` は費用取得途絶・予算・期間による停止、`trial_timeout` は時間制限、`target_rejected` は許可外接続先。未知SDK例外は `unexpected_error` とし本文を表示しない。子から親へも許可一覧にあるコードだけを渡す。

初回実Neon試験では `sslrootcert=system` の証明書照合が失敗した。TLS検証を緩めず、存在を確認した仮想環境のcertifi CA bundleへ固定する。`verify-full` と `channel_binding=require` は維持する。この修正後の実接続結果は親の試験記録で確認する。


## 旧台帳の接続先移行

既存の試験台帳を作り直さず、`migrate-target --ledger /Users/cnctor/.local/state/crm-capacity-trial-260910/ledger.json` を明示実行する。旧project `cnctor-crm-cap-trial-260910`・旧既定DBだけが移行対象。Firestoreの読取り・書込み・削除予約がすべて0、RPC履歴と返却文書数も0の場合に限る。別接続先・使用済み台帳・移行済み台帳は変更せず拒否する。

元の資源作成日時、Neonを含む累計予約、過去の費用証跡を保持して接続先を変更する。移行直後は費用を再照合待ちにして全実行を止める。移行後の時刻で、GCPを含む費用を `record-cost --cost-basis metered ...` により再照合するまで再開しない。旧Neon Freeの根拠で再開できず、14日の期限や累計値もリセットしない。この移行はローカル台帳だけを変更し、DB・認証・IAMを作成しない。
