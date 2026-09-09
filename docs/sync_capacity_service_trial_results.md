# 同期容量の専用資源と隔離試験記録

2026-09-10 主機CX。着手基準 `c046f0b`、製品基準 `d1b4060`。
本人「いけ」により、準備票の専用GCP・Neon作成と14日・合計20 USD管理予算を承認。
本番配備・監視登録・実Bot送信・資源撤去は対象外。

## 競合5の実結果（最新・2026-09-10）

[独立実行証跡](sync_capacity_stage_five_result.json)。診断改善後に未使用scope5を1回だけ実行。
36.110秒で不合格。12子中2成功・10失敗、全10件がFailedPrecondition / unexpected_error /
classified。従来のunknownから固定例外型まで取得できた。本文は保存しない。
観測SAによる事後読取りは12件=processing2/pending10、枠2占有/1空き、所有者/attempts一致。
待機10件は未取得で、満枠による正常待機だけではない。試験Python0を独立確認。

最終台帳：RPC1227/2000（残773）、read3887、write358、runtime5065秒、文書名77/100。
期限2026-09-24 04:57 JST・元created_at・累計予約を保持。推定拘束8 USD、実費未確定。
scope1/4/5不合格、2/3合格。5回成功条件未達。使用済みscopeの再実行はしない。

独立SECの再調査でも、例外型だけではガードの版照合、Firestore側の条件拒否、
claim先頭query、_atomic内のどの場所かは未確定。_atomicの8回/3秒/例外連鎖による
再送出のどれかも未確定。全体36.110秒を3秒打切りの証拠にしない。
次は固定発生場所・再試行終了理由・実試行回数/経過時間の最小診断を設計し、
ローカルで検証する。枠の追加は元期限/累計を保持する変更とレビューが必要。
再試行上限を根拠なく増やす案、台帳再作成、scope再利用は採らない。

## 競合4の失敗と診断改善（最新・2026-09-10）

[独立実競合の証跡](sync_capacity_stage_concurrency_results.json)を保存。
競合4は40.815秒で失敗。12子中3成功・9終了code1、全9件がunknown/unexpected_error。
終了後の試験Python0。競合5は開始せず、競合4の同scope再試行・回復・自動枠返却をしない。
実行前889→実行後1097RPC（+208）、read3351、write315、文書名64、runtime4285秒。
元Neon接続5/SQL24、created_at/期限は保持。推定8 USD枠であり実費は未確定。
事後の観測SAによる読取りは成功、保存12件（processing3/pending9）と3枠/所有者/試行回数の一致を独立照合。書込み増加0。観測後1099RPC（残901）、read3453。これは競合4を合格に変更する根拠ではない。

診断欠測の原因候補は2つあり、従来の証跡では区別不能：
1. stderr全体のJSON解析はSDKログが1行混ざると失敗する。
2. JSON正常でも未収録の型名はunknownに変換される。
初回/競合4がどちらだったかは断定しない。製品の比較更新再試行上限による失敗も未確定。

新diagnostics.pyで内部子の固定prefix+4項目JSONを取り出すよう改善。
非0終了だけを解析し、missing/invalid/unsupported/ambiguous/classifiedを分離。
生stderr・任意の例外本文・未収録の型名は保存せず、外側CLIのJSON形式は維持。
予算・接続先・成功判定・製品ロジックは変更しない。
独立SEC/品質B0W0、QA178成功10skip、既存警告1。合成12processの混在ログ分類も成功。
準備前終了のstderr個別原因はまだ欠測になる場合があり、既知WARNとして保持する。
この改善はローカル検証のみで、失敗競合の原因復元や実再試験合格を意味しない。

本人の具体的承認後、同じdiagnostic-review.txtを2社へ送信し回答を取得。
SHA-256 `1693cbb62fcf92d61cb6f0c5431cfa81df36cd233f14ead4594bf3e71eae3eeb`。
15,183文字。添付名を照合、添付後全文hashの再読照合は今回未実施。
旧タブは画面取得が停止したが、新規タブで正常化。プラグイン再インストールは不要だった。

- [Gemini Pro](https://gemini.google.com/app/00540e515b8c7417?hl=ja)：B0/W2。
- [Claude Opus 5・中](https://claude.ai/chat/d08de97a-a3ff-4ffe-b75c-093620bc8f0a)：B2/W8。

| 指摘 | 採否・限界 |
|---|---|
| Claude B1 有効型と未知コードの結合検証 | 不採用、独立SECも同意。Refusedコンストラクタでコードを許可リストへ正規化し、他例外はunexpected_error固定。通常生成経路では指摘の組合せを出さない |
| Claude B2 内部子環境設定の欠落 | 不採用、独立SECも同意。既存__main__のenvにCAPACITY_TRIAL_CHILD=1がありisolated_runへ渡す。差分資料に既存行が無いことを実装欠落とは扱わない |
| Claude W1 例外処理内import | 採用。最終例外出口の診断importを先頭へ移動。os/sysも先頭import済み |
| Claude W2 改行なしSDKログ | 採用。内部診断の前に改行を置き、実CLI子プロセスで分類を検証。外側JSON維持も追加検証 |
| Claude W3 大量stderrで待機 | 既知制約を保持。大量出力時は期限で不合格にする。競合4は非0終了でありtimeoutではなく、原因断定の根拠なし |
| Claude W4 45秒期限と50秒予約 | 不採用。時間予約は上限であり実所要と同値ではない。親期限を予約に合わせて延長しない |
| Claude W5/Gemini W2 unsupported分類 | 現状維持。未収録/未対応の分類として運用。型名や任意本文を保存しない |
| Claude W6 親例外分類の小さい許可リスト | 既知制約を保持。子診断分類と実行段階の親例外分類は別。準備前の原因欠測を解決済みとはしない |
| Claude W7 台帳記録失敗による例外置換 | 不採用。記録保存失敗を隠す提案は採らない。元原因が置き換わる限界は既記録 |
| Claude W8 ready前終了/部分行 | 既知制約を保持。準備段階欠測の可能性、外側60秒による停止を維持。競合4はcollecting段階 |
| Gemini W1/Claude I2 完全一致schema | 現状維持。V1固定形式であり追加項目は拒否する。将来は送受信の版を揃えて変更する |

重大指摘の採否確定後、追加修正は独立SEC/品質B0W0、QA180成功10skip・既存警告1で確認。
今回の外部回答は追加修正前の資料への評価。追加修正後の実診断結果は上記競合5を参照。
5回成功の条件は未達。大規模/実worker復旧/監視/復元/outboxは未完了。

## 第2段階の承認反映・実移行（2026-09-10）

本人がGeminiへの具体的資料送信を承認。同じstage-review.txt（SHA-256
`4f03c0a7e40a8f428bed5feaf6fa904db9d7552bd4bba664bfb3026e30736628`）を添付し、
[Gemini 3.1 Pro回答](https://gemini.google.com/app/be4bb308a193a06f?hl=ja)を取得。
B0/W1。W1は費用記録2 USD以上で移行拒否する意図した停止線であり、既存境界試験で確認済み。
添付前の元ファイルhash・添付名を照合。Geminiの添付カードは本文プレビューを開かず、
アップロード後全文hashの再読照合は未実施（Claude添付では全文hash一致済み）。
回答の安全性に関する評価は静的レビューであり、実サービスの動作保証とは扱わない。

[固定12scopeの実読取り・移行証跡](sync_capacity_stage_inventory.json)：
元台帳の第1段階枠で12回inspect-stateを実行し、24RPC/1224readを追加予約。
9scope文書と41job（計50文書）が従来結果に一致し、競合4/5とpermission親scopeは不存在。
台帳の文書名予約51は、403拒否済みpermission文書の名前1件を含む。
これは固定scopeの照合であり、未知collectionの全件列挙ではない。監査ログ0件も外部書込みなしの
証明には使わず、専用DB作成以来本試験以外の投入を行っていない作業履歴と合わせた確認。

短期tokenを2 SA分更新し、キーチェーンへ標準入力で保存・読み戻し一致。秘密は証跡に残さない。
単一調整役、試験Python0を再確認し、元台帳を第2段階へ一度だけ移行。
created_at・reserved・rpc_calls・returned_documents・cost・cost_refresh_requiredが前後一致し、
旧upper_bound全体と履歴snapshotも完全一致。期限9/24 04:57 JSTは不変。
移行直後889RPC/2000、read2952/10000、文書名51/100、推定拘束8 USD。

## 第2段階の準備（2026-09-10・実適用前）

実台帳は元の小規模枠のまま、865 RPC使用・残135、元期限2026-09-24 04:57 JSTを保持。
今回のFireStore文書RPCは0件。管理APIの読取りとNeon契約/表示の再確認だけを実施した。

- 競合1の子エラーは当時破棄され、原因復元は不可能。保存JSONの再集計はprocessing3/pending9、所有者/枠一致だが不合格を維持。
- 準備前終了・不正JSON・timeoutでも段階と固定分類を残す改善を追加。準備前のstderr個別原因はまだ拾わず、JSONDecodeErrorになる経路が残る（WARN）。未確定を成功に変えない。
- 第2段階の移行コマンドを用意。累計2000RPC/read10000/100文書/推定8 USD。旧4 USDは内包し、元期限・予約・費用証跡を保持。旧状態snapshotを台帳内に保存し再移行を拒否。詳細はscripts/capacity_trial/README.md。
- ローカル独立QAは最終167成功・10skip。初回sandboxの2失敗はps実行制約、権限付き再実行で解消。実サービス検証ではない。SEC B0/W0、品質W2件は手順追記・キー名改善で解消。
- [移行前の読み取り証跡](sync_capacity_stage_preflight.json)：DB Standard/us-east4/PITR無効/backup予定0/TTL0、試験Python0。Neon Free/接続0/Idle、残枠表示0/100CUh・0/0.5GB・0/5GB（遅延あり）。料金は公式画面のus-east4で再照合。
- 実移行・競合4/5・大規模走査・復旧/復元・outbox試験は未実施。第2段階もscan/Neon/削除を許可しない。別経路投入がないことの照合は実移行直前に残る。

Gemini資料添付はauto-reviewが2回拒否。理由は「内部コード・非公開GCP/Neon接続情報を含む具体的payloadのGemini送信承認がない」。本人提示の一般ルールを示した再審査も拒否のため再送しない。
送信待ち資料は `/Users/cnctor/.local/state/crm-capacity-trial-260910/stage-review.txt`。
44,833文字、SHA-256 `4f03c0a7e40a8f428bed5feaf6fa904db9d7552bd4bba664bfb3026e30736628`。
秘密パターン0・顧客情報なし。空白除去後SHA-256は `8721862e4aed1d2d9e86b2f8ae6e6bd1c784cab3c38c224ea61f8a526e42bc03`。
Claudeは明示承認済みのため同一ファイルを添付し、プレビュー全文hash一致後にOpus 5・中へ送信済み。
[Claude回答](https://claude.ai/chat/92d37fcc-9c88-4424-ad71-3a975539b25f)は取得完了。B2件は独立SECと次の根拠で不採用。

| 指摘 | 採否・限界 |
|---|---|
| B1：移行はmetered必須 | 不採用。開始以来の累計を含む別根拠の推定上限8 USDで制御する設計。実費0を登録しない。資源/単価/別経路なしの前提照合は引き続き必要 |
| B2：16000 writesは0.01584 USDを超える | 計算誤り。16000×0.099/100000=0.01584。1600件へ減らす提案の根拠なし |
| B2：返却数は未拘束 | 不採用。request limit分のread事前予約が正本。Firestoreが要求limitを守る契約が前提。バイト数の独立実測上限ではない |
| W：確認フラグを分割 | 現状維持。READMEの全項目を含む1つの明示宣言。フラグを増やしても外部検証の代行にはならない |
| W：移行と実行の排他 | 運用制約として保持。単一調整役・全process停止確認後だけ移行。並行移行を機械的に阻止する実行中markerは未実装 |
| W：台帳記録失敗を握り潰す | 不採用。記録保存失敗を隠さず失敗として返す。元の原因分類が置き換わる限界は残る |
| W：停止確認前falseの定数 | 観測時点を明記済み。独立した終了確認結果とは扱わない |
| W：失敗2欄の重複/履歴伸長 | 事象は詳細と実行段階の2欄。件数集計時は単純合算しない。小規模試験用で無制限の再試行はしない |
| W：台帳欠損のKeyError | 公開CLIはunexpected_errorの固定分類で失敗。欠測を0で補わない |
| W：親directory fsyncなし | 既存台帳の耐電源断保証の限界として記録。今回、実行前予約が電源断時にも完全保証されるとは表明しない |
| W：Popen失敗時の子数過少 | Popenでhandleを取得できた子の数。全起動成功や独立停止証跡と扱わない |
| I：費用2 USD到達の停止テスト | 採用。stage2でcost2+reserved8に達すると停止し台帳不変を確認。対象追加8件成功 |
| I：JSONDecodeErrorをRefusedへ変換 | 現状維持。CLIが例外本文を出さず型/固定コードだけ出力。準備前stderr原因は欠測のまま明記 |

最終動作差分はClaude送信版と同じ。送信後の変更は費用2 USD境界テストと記録・説明の追加。
Geminiレビューは未実施のため、2社レビュー通過済みとはしない。


## 現在の到達点

**隔離試験全体は未完了。本人の限定IAM明示承認後、専用DB権限・短期認証を付与し小規模実試験を実施。**
専用NeonはFreeで作成済み、本番アプリとの接続は0件。
本人「claude送信OK」後、Claude Opus 5・中の回答を取得し、必要な改善を反映。
接続先変更時に元の期限・使用量を保つ移行機能を追加。実Firestoreのsmokeと観測SAの403拒否は成功。請求明細は当日未反映で、実費0とせず小規模推定枠で実行中。
作成枠+1申請はAPI無料を確認したが、組織の上限閲覧権限が不足し未送信。

## 資源の実測

| 対象 | 確認結果 |
|---|---|
| GCP | 本人指定 `actionpoint-autocalc`（番号251479293924）。旧候補 `cnctor-crm-cap-trial-260910` は未作成 |
| 作成operation | 初回 `operations/create_project.global.6845472103091097380`、組織明示再試行 `operations/create_project.global.5700522104416181967`。両方code 8 / allotted project quota超過 |
| 一覧照合 | 閲覧可能ACTIVE projectは30件。上限が30である証明ではない。組織 `cnctor.jp` は閲覧可能 |
| GCP課金先 | 既存課金先open=true、通貨JPY。指定projectは元からbillingEnabled=true |
| Firestore | `crm-capacity-trial-260910` / us-east4 / Standard / Native / 削除保護あり。2026-09-09T20:30:00.223688Z作成、PITR無効 |
| 既存DB確認 | API有効化後、作成直前の一覧0件。既存の別用途データは読まない |
| API変更 | firestore.googleapis.com、iamcredentials.googleapis.com、cloudquotas.googleapis.com有効化成功 |
| SA / custom role | 実行・観測2 SA、DB用2 role、短期token用1 role。本人承認後、専用DB限定2 binding・各SA上の本人token発行bindingを期限付きで付与 |
| index / 文書 | state/available_at複合索引1件がREADY（CICAgOjXh4EK）。smoke合成文書の保存・完了確認済み。短期SA token2件発行成功、キーチェーン保存のみ |
| Neon名 | `crm-capacity-trial-260910` |
| Neon project | `fragrant-silence-24771784` |
| branch | `br-silent-king-avubki1s`（新規空projectのmain。本番branch複製なし） |
| endpoint | `ep-shiny-silence-avof39pi` |
| host | `ep-shiny-silence-avof39pi.c-11.us-east-1.aws.neon.tech` |
| database/role | `neondb` / `neondb_owner` |
| Vercel資源 | `store_xW8Esi5eapEwgREA`、接続project一覧 `[]` |
| 契約 | `free_v3` / Free。追加有料契約なし |
| region/compute | AWS us-east-1、min=max 0.25 CU、自動休止設定5分。試験2分後はACTIVE、後続のDashboardでIdle到達を確認 |
| 使用枠表示 | 作成直後0/100 CU時間、0/0.5 GB、転送0/5 GB。最大1時間の表示遅延あり |
| 作成日時 | 2026-09-09 19:57 UTC頃（JST 09-10 04:57）。期間管理は19:57 UTCを保守的な起点にする |

CLI `vercel integration add neon --plan free_v3 --metadata region=iad1 --metadata auth=false --no-connect --no-env-pull` で作成。
実行後 `integration list --all` と `integration-resource inspect crm-capacity-trial-260910` で
Free/available/接続0を読み戻した。既存本番Neonの接続設定は変えていない。
CLIが更新したローカルNeonスキルとreferencesは `/private/tmp/capacity-trial-cli-skills-a8sx_8u2` へ退避し、元の版に復元。
接続秘密はmacOSキーチェーン `capacity-trial-neon-dsn` / `neondb_owner` のみ。
ブラウザ補助画面のVercelは2FA要求だったが、既存認証CLIで作成と照合を完了し、2FA操作は不要だった。

## us-east4料金の照合

[Firestore Standard公式料金](https://cloud.google.com/firestore/pricing)の地域選択で
Northern Virginia (us-east4) に切り替えた表示。2026-09-10 JST確認、割引なしUSD。

| 費目 | 単価 |
|---|---:|
| 読取り10万文書 | $0.033 |
| 書込み10万文書 | $0.099 |
| 削除10万文書 | $0.011 |
| 保存GiB・時 | $0.000135616 |
| バックアップGiB・時 | $0.000045205 |
| 復元GiB | $0.22 |

予定上限の文書操作は `30×0.033 + 5×0.099 + 2×0.011 = $1.507`。
元票のIowa参考値$1.37より10%増。無料枠は控除しない。
索引/保存/転送は別。1 GiBを14日保存する参考額は$0.045567、
1 GiBバックアップ14日保存は$0.015189、1 GiB復元は$0.22。
本試験では小さい合成文書を使う計画で、実行量を上限に収めた上で各バッチ前後に利用量を照合する。
JPY請求は契約通貨のSKU/税に従い、これらのUSD管理値を円の確定請求額と扱わない。
DB作成応答は `freeTier:true`。最初に作ったDBの無料枠表示を確認した事実であり、大量試験の無料保証ではない。無料枠を引かず見積もり、請求実測の取得や費用停止機能の実サービス確認は未実施。

## GCPの再開条件

本人の「gcpはactionpoint-autocalcを使って」で接続先を変更。専用project新規作成待ちは解消。
既存用途のサービスアカウント2件は変更せず、専用named DBと専用SAを追加した。
API有効化は一度自動承認レビューに拒否されたが、先の14日/20 USD承認と接続先指定を
明記した再審査が通り実行済み。DB作成・SA2件・custom role2件も成功。
[付与前の資源読み戻し証跡](sync_capacity_gcp_resource_result.json)：当時は専用SAを含むproject直接付与0、各SA上のbindingも0。継承を含む実効権限全体の否定ではない。

**当初IAM付与は2回とも自動承認レビューが拒否した。その後、本人の「承認するよ」を受けて付与・読み戻しまで完了。**
理由は「準備文書だけでは特定SAへの権限・対象・期限の明示承認を満たさない」。
本人の認証で直接文書を操作する代替は、専用SAの隔離検証を省くため採用しない。
本人が明示承認した権限：

| 対象 | 権限と範囲 |
|---|---|
| `capacity-trial-runner@actionpoint-autocalc.iam.gserviceaccount.com` | `capacityTrialRunner260910`：datastore.entities の get/list/create/update。専用DBだけ |
| `capacity-trial-observer@actionpoint-autocalc.iam.gserviceaccount.com` | `capacityTrialObserver260910`：get/list。専用DBだけ |
| `kanazawa@cnctor.jp` | 上記2 SAの短期token発行だけ（iam.serviceAccounts.getAccessToken）。project全体の代理権限は付与しない |

DB権限の条件は `resource.name=="projects/actionpoint-autocalc/databases/crm-capacity-trial-260910"`。
全付与の有効期限は `request.time < timestamp("2026-09-23T19:57:00Z")`（JST 9/24 04:57）。
削除・DB管理・既存アプリへの権限を含めない。token発行用は `capacityTrialToken260910`（getAccessTokenだけ）。
[付与後IAM証跡](sync_capacity_gcp_iam_result.json)でDB限定・本人限定・期限を照合済み。
30分の短期tokenを2件発行し、キーチェーンの専用serviceだけに保存した。静的鍵は発行しない。

## 請求反映遅延と小規模枠

[試験前費用表示](sync_capacity_cost_preflight.json)。GCPレポートをprojectで絞り、表示0円を確認したが、
反映済期間は9/8まで。本試験資源は9/10作成なので当日費用は未確定。
取得時刻を現在にしてmetered=0と登録することはしない。独立SECも同判断。
[Cloud Monitoring読取り](sync_capacity_gcp_usage_preflight.json)も4指標でseriesなし。
これは観測点0件であって、実際の操作数・保存量0の計測ではない。
最初の標準PythonではCA検証エラー、既存certifi CAを指定した再取得は成功（検証無効化なし）。
Neon専用projectはFree、0/100 CUh・0/0.5 GB・0/5 GB表示、Idleを確認。

請求反映前に許可するのは、小規模合成ケースだけを対象とする別根拠
`reserved-upper-bound` の実装・独立検証後。meteredの停止条件は緩和しない。
大規模scan・新Neon試験・バックアップ/復元はこの根拠で実行しない。
同じ元台帳で期限・予約を保持し、累計文書名100、文書read予約5000、RPC1000を拘束する実装を有効化済み。
無料枠は控除せず4 USDを予約する。実測費用でも、撤去までの生涯費用保証でもない。

[Firestore上限](https://firebase.google.com/docs/firestore/quotas)の文書1 MiB・索引8 MiB・
索引entry4万/文書、[価格](https://cloud.google.com/firestore/pricing)のus-east4単価と
下り最大0.23 USD/GiBを使う保守式：

| 費目 | 枠全体の予約式 | USD |
|---|---|---:|
| 14日保存 | 100×9 MiB×336h×0.000135616/GiBh | 0.0400491 |
| 索引read | 1000 RPC×4000 read units×0.033/10万 | 1.32 |
| 文書read | 5000×0.033/10万 | 0.00165 |
| 文書write | 1000 RPC×8文書×0.099/10万 | 0.00792 |
| 下り | 5000文書×2 MiB×0.23/GiB | 2.24609375 |
| 計算合計 | 上記合計 | 3.61571285 |
| 確保する管理枠 | 付随応答等の予備を含む | 4 |

文書4 KiB・commit8文書/32 KiB・query形状の制限を合わせ、送信前にprocess間で予約する。
転送2 MiB/文書はサービス上限1 MiBと応答包み用の保守枠。空queryの最低readも予約対象。
追加課金要因のPITRは無効、backup schedule0・TTL設定0を管理APIで確認。
別経路投入、計算できない費目、台帳/前提の欠落では停止する。

## 作成枠1件の追加申請

本人は「無料ならよい」と承認。[Cloud Quotas公式料金](https://cloud.google.com/quotas/pricing)
でAPI利用は無料と確認。[公式注意](https://docs.cloud.google.com/docs/quotas/overview)では
増枠審査に前払いを求める場合があるため、支払い要求には応じない。

`cloudquotas.googleapis.com`を有効化後、組織469895005234のResource Manager quotaを
本人アカウントで読み取ったが `cloudquotas.quotas.get` のIAM権限不足で拒否。
現在上限と既存申請を確認できないため、上限値を推測して送信しない。申請は未送信。
組織管理者の上限閲覧・変更権限、または管理者による+1申請が必要。自分への組織権限付与はしない。
旧project作成失敗のoperationは上表の2件。今回は既存project使用で試験先は確保済み。

## 未完了の試験

12process×3枠×5回の合格・実worker強制停止復旧・9回全件観測・持続負荷・監視合成受信口・別DB復元、
既存同期/outboxと実Neonの組合せは未検証。
新runnerにまだ実装していない試験はREADMEに明記する。
本番容量保証・本番7日観測も未達。NeonだけのSELECT/排他試験で代替しない。

## レビューとローカル検証

独立SEC最終BLOCKER 0 / WARN 0、品質最終BLOCKER 0 / WARN 0 / INFO 1。
旧版21772c6のQAは `tests/sync_capacity` 115成功・10skip（emulator未起動）、専用ガード37件はその内数。
前回8a4bdf6は132成功・10skip（専用54件は内数）。独立SEC/品質の再レビューB0/W0。QAの拒否テスト改善も反映済み。
台帳12独立process×100予約＝1,200件、欠落0。異常値4ケースと時刻境界3ケース、
launcher正常/認証失敗/空token/子失敗/timeout/JSON不正の6ケースは追加模擬検証。
実60秒kill・実Firestoreの確認ではない。製品コード変更なしのため全製品pytestは再実行しない。

修正した点：過去completedを今回のsmoke成功と扱わない、全件query上限超過を部分成功にしない、
offsetによる読取り予約不足を拒否、固定原因コードだけ親へ伝播、Neon証明書bundleを明示。
エラー全文や接続文字列を証跡へ出さない。操作数予約は実請求操作数とは区別する。

[Gemini 3.1 Proレビュー](https://gemini.google.com/app/80404ebdd73630d4?hl=ja)は取得済み。
同一レビュー資料 `capacity-trial-review.txt` は33,856文字、
SHA-256 `8a4ff660060d3e8c4a20d2ea9bf23c3330d431a243f27a61d46c66a26d26ec7b`。
認証秘密パターン0、顧客データなし。依頼文96文字（空白除去）、照合hash3454429646一致。

| 外部指摘 | 採否 |
|---|---|
| BLOCKER：子の失敗原因が消える | 原因識別を採用、独立品質指摘と同じ。固定error_codeを追加済み。提案のstderr生表示は秘密露出のため不採用 |
| WARN：55秒SDK timeoutが60秒判定より先 | 不採用。SDK55秒と親60秒で失敗を閉じる設計。65秒への延長はしない |
| WARN：空envではsystem CA解決に失敗しうる | 実Neonで証明書失敗を確認。既存certifi bundle固定を採用。TLS verify-full/channel_bindingは維持 |
| INFO：StructuredQuery型の混在 | 現SDKのRunQueryRequestへ変換・99byteシリアライズ成功。将来のSDK変更の仮説だけで書換えない |

[Claude Opus 5・中レビュー](https://claude.ai/chat/3c0150f4-1f48-4033-bc1d-3dad261990ca)は本人の明示送信承認後に取得。
38,130文字、原文SHA-256 `910d1d66f889d2f049bea3a66795bec9362f82e0cd7058f749bd623ffef97ca0`。
貼付は添付へ自動変換されたため、添付本文を開いて空白除去後のSHA-256
`1588ed4df061ab6508cce220753ef12e82018d2e4d3912288261ec8df3046197` の一致を確認して送信。
本文列挙はB3/W7/I6（回答冒頭のW6と不一致）。独立SECと採否を整理した。

| 指摘 | 採否 |
|---|---|
| B1：Neon証跡の接続数/SQL数/closeが定数 | 採用。成功query数・接続配列数を測定し、ExitStack後のclosedを確認。修正版の実Neonは未実行 |
| B2：initializeが既存scopeを上書き | 誤検知。既存scopeは検証のみ、変更0のcommitは送信なし。ただし使用済みjobsの検査はinitialize前へ改善 |
| B3：全queryのlimitに番兵を追加 | 不採用。全件observeはlimitなしで番兵有効。全limited queryへ適用するとclaim(limit20)が待機21件で停止する |
| W1：未知属性でRefused | 拒否を維持。必要な非RPC属性でSDKが失敗する事実は未確認。実Firestore接続時の確認事項 |
| W2：finally台帳例外を握り潰す | 不採用。正常読取り後の台帳保存失敗まで成功とするため。固定原因分類の失敗として閉じる |
| W3：scanをobserver限定 | 採用。認証取得前に拒否 |
| W4：scan_seconds欠落時0にする | 不採用。observation.pyの返却キーは存在。計測欠落を0秒成功にしない |
| W5：子stdoutの最後のJSONだけ拾う | 不採用。余分な出力も異常として拒否する |
| W6：HOME空検査を緩和 | 不採用。汚染時は接続前拒否する現仕様を維持 |
| W7：certifi未導入時の原因分類 | 採用。Refusedの固定分類へ変換 |
| INFO群 | 予約と実測・macOS依存・未実装は明記。費用根拠は新GCPでは常にmeteredを要求しFree根拠への逆戻りも拒否 |

## 費用と停止の証跡

持続用台帳は `/Users/cnctor/.local/state/crm-capacity-trial-260910/ledger.json`。
キーチェーンに秘密、台帳に非秘密の件数/費用根拠のみを置く。
`migrate-target`を実台帳へ適用済み。created_at1788983820、予約145秒/接続5/SQL24を保持し、費用再照合待ちで停止中。
Neon Freeの管理値0は `basis=free-plan-verified` で区別し、請求実測0とは扱わない。
Firestore作成前のNeon試験時点では、新規有料GCP資源がないこと・Neon Free枠内であることを確認して小規模試験のみを許可した。
Firestore作成後はこの根拠を流用せず、全資源の利用量を照合する。移行後は費用再照合まで停止し、以後もFree根拠への逆戻りを拒否する。

初回Neon試験は証明書OperationalErrorで失敗（実行成功ではない）。
原因分類の追加接続1回はSQLなし、秘密を出さずcertificate=trueのみ取得。
この手動診断の予約は実行後に台帳へ計上したため、runner内の事前予約と区別する。
初回と診断の予約は戻さず、再試験分と累積する。

## 専用Neon実試験の結果

[機械可読の試験証跡](sync_capacity_neon_probe_result.json)。
UTC 2026-09-09T20:15:52.472813+00:00 ～ 2026-09-09T20:15:58.235618+00:00、所要5.763秒。
2独立接続・7 SQL、専用database/role一致、別backend、session鍵の競合拒否と解放後取得、
両接続はExitStackの正常終了を確認。旧証跡のconnections_closedは固定Trueでclosed属性の実測ではなかった。SQL成功イベントは7件で、各assertの通過を確認した。修正版は実測counterとclosed属性を検証するが、実Neon再試験は未実施。
実証明書検証・認証・SQL・排他は成功。業務テーブル作成/書込みはなし。
強制停止・本番容量・既存同期/outbox・大量負荷の試験ではない。

実行版は親コミットc046f0b＋証跡内の各ファイルSHA-256で固定。
SDK: psycopg/psycopg-binary 3.3.4、libpq 18.0、google-cloud-firestore 2.30.0、
google-api-core 2.36.0、certifi 2026.7.22。

Claudeへ送信済みの旧実装レビュー資料は `/Users/cnctor/.local/state/crm-capacity-trial-260910/claude-review-pending.txt`。
38,130文字、SHA-256 `910d1d66f889d2f049bea3a66795bec9362f82e0cd7058f749bd623ffef97ca0`。
コードの重複保管を避け、送信bundle自体はgitへ入れない。生成元はscripts/capacity_trialと専用テスト。

終了確認：専用試験Pythonプロセスは一覧照合で0件。Neon側は再読込したComputes画面で
ACTIVE（試験終了約2分後）、0.25 CU。5分無操作の自動休止設定は確認済みだが、
この記録時点で休止への到達は未確認。古い画面のSUSPENDED表示を終了証跡には使わない。
台帳予約累計：145秒、接続5本、SQL24本。失敗分を含む安全側の予約で、実使用数ではない。


## 今回の小規模実装の独立検証（2026-09-10）

12独立processの同期開始、5操作の保存直後の応答喪失、100文書/1000RPC/5000readの共有予約、
観測SAの実403確認を追加。IAM試験は送信前にUUID付き停止を永続化し、同じ試験の403確認時だけ解除する。
通常例外・強制終了・予想外の書込み成功では停止が残る。製品コードは変更しない。
独立SECはBLOCKER 0/WARN 0、関連テスト151成功/10skip（専用73件は内数）。
品質指摘2件は通常費用根拠と小規模根拠の違い、解除不可・台帳再作成禁止をREADMEへ追記して解消。
QAは12実processのIPC正常/不正準備/不正結果/準備前終了と、親・孫の停止を検証。
停止試験は期限を0.5秒へ短縮した実process検証であり、実サービス60秒待ちの試験ではない。

外部送信は秘密・顧客情報なしを検査した全文51,166文字。空白除去後39,851文字、
SHA-256 `54caeb0504f38d6059a8a60120ae136dcbb67b42d74c03e3df0d0c95414f4768` を貼付後照合。
Gemini ProはBLOCKER 0/WARN 0/INFO 3。直接子起動は既存環境ガードで拒否、CASは型捕捉、
台帳update戻り値は不使用なので追加変更不要。静的レビューの賛辞を実サービス成功の証拠とは扱わない。
Claude Opus 5・中の今回回答はBLOCKER 1/WARN 7/INFO 7（本文の番号で集計）。
B1は送信前拒否でも不明停止を記録する点。汎用exceptでの追加停止を削除し、RPC直前markerだけを残す方式へ修正。
W1の例外code・子returncodeからの証跡、W2の未回収PID保持→group停止→回収、W3のtimeout実引数、
W4の終了期限分類を採用。競合例外をclaimなしへ握り潰す提案は、失敗を成功へ混ぜるため不採用。
W5は製品がslots全体のmap更新と明示時刻を使い、ドット/transformを使わないことをソース照合。
W6は明示limitを既存確認/claimにも使うため一律+1は不採用。全件observeはlimitなしで番兵あり。
W7の台帳例外握り潰し・未承認属性委譲も不採用。欠測の成功化やガード緩和を避け、失敗で停止する。
INFOの逐次合図・単一調整役・smoke先行・推定枠の実効的制御をREADMEへ補足。


## 実Firestore接続で見つかった差異と改善

- 起動4回は成功せず、書込み前に停止。run_query予約4回を保持し、元の台帳をリセットしていない。
- このMacでは終了済みgroupへのkillpgがEPERMとなった。未回収PIDを保持し、EPERM時だけpsで
  親PID=PGIDのゾンビ存在・非ゾンビ0を照合してから回収。psが拒否/失敗なら成功扱いしない。
  通常sandboxではps自体が拒否。許可環境で正常終了・実孫timeout・親異常終了を検証した。
- 元の接続例外はServiceUnavailable/DNS。公式[gRPC環境変数](https://github.com/grpc/grpc/blob/master/doc/environment_variables.md)
  のnative resolverを子・孫の環境に固定し、他値をガードで拒否。変更後smokeは13.184秒で成功。
- permission初回は保存eventをid直下と誤って期待してローカル拒否。製品はbody/headersを保存するため、
  正本submission().eventと照合するよう試験と模擬入力を修正。書込み予約前の拒否でhaltなし。
  再試験は1.912秒で合成読取りとサーバ403を確認。製品の保存形式を変更していない。

成功・失敗を含むケースは[小規模実試験JSON](sync_capacity_small_trial_results.json)へ記録する。


## 実試験の今回集計

| ケース | 結果 | 実地照合 |
|---|---|---|
| smoke | 成功 | 合成1件completed、重複・内容競合拒否、全件再読1件 |
| 観測SA | 成功 | 合成smoke読取り成功、固定createは実403 |
| 応答喪失5操作 | 全5件成功 | 各commit1回、状態再読一致。claimのみ枠保持 |
| 競合1 | 不合格 | child_failed。事後読取りは処理中3/待機9、3枠と所有者一致 |
| 競合2 | 成功 | 12子正常終了、3取得・12保存、所有者/枠一致、38.118秒 |
| 競合3 | 成功 | 12子正常終了、3取得・12保存、所有者/枠一致、32.110秒 |

競合1の子stderrは破棄していたため原因型が欠測。3枠が守られた結果だけを取り出して合格にしない。
診断改善として、全子の終了後に固定error_type/error_codeだけを台帳へ記録する。
任意の例外本文は保存しない。子失敗が1件でもあれば試験は必ず失敗とする。
12実プロセスの全失敗・秘密除去を含む最終独立SEC/QAは158成功/10skip、BLOCKER 0/WARN 0。
実行環境と試験側の不備を除去した後も、製品の競合再試行上限や未確定のSDK原因を成功へ丸めない。


競合4/5は未実行。累計865/1000 RPC、残135は直近成功1回210 RPCより少ないため、新規競合を開始しない。
枠を増やす・新台帳へ移す・使用済scopeを再利用する方法で回避しない。競合1は不合格のまま、5回合格条件は未達。

台帳の文書名予約51件（拒否されたpermission createを含む）、文書read予約1728、write予約242、
累計実行時間予約2905秒、元Neon接続予約5/SQL24を保持。これらは実請求件数ではない。
計算上限4 USDを拘束した小規模枠、実請求額は当日未反映で未確定。14日期限はJST2026-09-24 04:57のまま。
資源・保存slotを残し、自動再開/回収/撤去/本番配備はしていない。

次段階は第1回の失敗原因切り分け、元期限/累計を保った予算ガードの段階移行と残試験。
100k全件走査・持続負荷・実worker強制終了/復旧・監視/復元・同期outbox実Neon組合せは未完了。
実サービスで一部成功した結果を、本番容量保証や全隔離試験完了として扱わない。


## 最終差分の追レビュー

差分13,954文字を両モデルへ送信前照合（空白除去SHA-256
`dd316df149b2b9acd2f5609c98866b71fc229810ff5cd6b92257ecdcd6dba02a`）。
Gemini ProはB0/W0。Claude Opus5・中はB0/W5/I5、前回B1の解消を確認。
W2のwaitid終了PID一致、W3の早期終了BrokenPipeと固定子エラーの維持を採用。
W1は停止確認の失敗を無視する提案を不採用。停止未確認を優先して失敗とするため、元のtimeout分類が
覆われる場合が残る。固定コードだけから停止完了や原因を断定しない。
W4の生stderr一時ファイル保存は認証情報混入の回避方針から不採用。SDKが大量stderrを出すと準備段階が
45秒の期限失敗になる可能性は残る。今回成功2回では未発生、誤成功ではなく停止になる。
W5は全子communicateで終了回収→非0が1件でも例外→12結果/PID照合後だけ成功、という制御経路で保証。
常にtrueになる成功条件を独立観測であるとは表現しない。個別終了コードは実processから取得している。

最後のlauncher2点はローカル再検証のみ。実サービスの結果はその直前版によるもので、最終版の実再試験は行わない。
小規模枠を消費して既使用scopeを再実行することを避ける。次の再開時は元台帳・期限・残枠のまま判断する。

最終独立SEC/QAは159成功/10skip、BLOCKER 0/WARN 0。既存Starlette非推奨警告1件。
