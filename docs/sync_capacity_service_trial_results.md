# 同期容量の専用資源と隔離試験記録

2026-09-10 主機CX。着手基準 `c046f0b`、製品基準 `d1b4060`。
本人「いけ」により、準備票の専用GCP・Neon作成と14日・合計20 USD管理予算を承認。
本番配備・監視登録・実Bot送信・資源撤去は対象外。

## 現在の到達点

**隔離試験全体は未完了。GCP project作成がプロジェクト数上限で拒否された。**
専用NeonはFreeで作成済み、本番アプリとの接続は0件。
ローカル115件成功、独立レビュー指摘を修正し、専用Neonの2接続・7 SQL・排他確認は成功。Claudeレビューは送信承認待ち。

## 資源の実測

| 対象 | 確認結果 |
|---|---|
| GCP候補 | `cnctor-crm-cap-trial-260910` 未作成 |
| 作成operation | 初回 `operations/create_project.global.6845472103091097380`、組織明示再試行 `operations/create_project.global.5700522104416181967`。両方code 8 / allotted project quota超過 |
| 一覧照合 | 閲覧可能ACTIVE projectは30件。上限が30である証明ではない。組織 `cnctor.jp` は閲覧可能 |
| GCP課金先 | 既存課金先open=true、通貨JPY。試験projectへの関連付けなし |
| Firestore/SA/IAM/index | 作成・変更なし。料金表閲覧でAPI有効化はしない |
| Neon名 | `crm-capacity-trial-260910` |
| Neon project | `fragrant-silence-24771784` |
| branch | `br-silent-king-avubki1s`（新規空projectのmain。本番branch複製なし） |
| endpoint | `ep-shiny-silence-avof39pi` |
| host | `ep-shiny-silence-avof39pi.c-11.us-east-1.aws.neon.tech` |
| database/role | `neondb` / `neondb_owner` |
| Vercel資源 | `store_xW8Esi5eapEwgREA`、接続project一覧 `[]` |
| 契約 | `free_v3` / Free。追加有料契約なし |
| region/compute | AWS us-east-1、min=max 0.25 CU、自動休止設定5分。試験2分後はACTIVE、休止到達は未確認 |
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
GCP資源は未作成なので、請求実測の取得や費用停止機能の実サービス確認は未実施。

## GCPの再開条件

Google側のproject quotaが作成を拒否しており、予算承認とは別のサービス制約。
既存project削除、他アカウント利用、本番projectへの試験DB同居はしない。
[Google公式の作成数上限手順](https://docs.cloud.google.com/resource-manager/docs/creating-managing-projects)
に沿って、本人または組織管理者による追加1 project分の上限確保が必要。
申請送信は未実施（第三者への申請を今回の資源作成承認と同一視しない）。

申請用の要点：

> CRM同期の隔離容量試験に専用projectを1件追加したい。
> project ID候補: cnctor-crm-cap-trial-260910。
> 合成データのみ、最大10万文書、期間14日、GCP/Neon合計管理予算20 USD。
> 既存本番から分離するため、追加作成枠を1件希望する。
> projects.createのoperationは上記参照、QuotaFailureで未作成。

上限解消後は同じ承認範囲で再開できる。名前・地域・予算を再承認待ちに戻さない。

## 未完了の試験

Firestore実IAM拒否・index query・保存/再起動・12process×3枠×5回・
5操作commit応答喪失・強制停止復旧・9回全件観測・持続負荷・監視合成受信口・別DB復元、
既存同期/outboxと実Neonの組合せは未検証。
新runnerにまだ実装していない試験はREADMEに明記する。
本番容量保証・本番7日観測も未達。NeonだけのSELECT/排他試験で代替しない。

## レビューとローカル検証

独立SEC最終BLOCKER 0 / WARN 0、品質最終BLOCKER 0 / WARN 0 / INFO 1。
QAは `tests/sync_capacity` 115成功・10skip（emulator未起動）、専用ガード37件はその内数。
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

Claude Opus 5・思考量中を選択したが、同一コードの貼り付けが自動承認レビューにより拒否。
理由は「内部コード全文の外部送信には送信直前の確認が必要」。貼り付け・送信・回答取得は未実施。
回避経路で送信しない。最終版資料を準備し、本人の直前承認後にClaudeへ送信する。

## 費用と停止の証跡

持続用台帳は `/Users/cnctor/.local/state/crm-capacity-trial-260910/ledger.json`。
キーチェーンに秘密、台帳に非秘密の件数/費用根拠のみを置く。
Neon Freeの管理値0は `basis=free-plan-verified` で区別し、請求実測0とは扱わない。
新規有料GCP資源がないこと・Neon Free枠内であることを確認して小規模試験のみを許可。
将来Firestoreを作成したらこの根拠を流用せず、全資源の利用量を照合する。

初回Neon試験は証明書OperationalErrorで失敗（実行成功ではない）。
原因分類の追加接続1回はSQLなし、秘密を出さずcertificate=trueのみ取得。
この手動診断の予約は実行後に台帳へ計上したため、runner内の事前予約と区別する。
初回と診断の予約は戻さず、再試験分と累積する。

## 専用Neon実試験の結果

[機械可読の試験証跡](sync_capacity_neon_probe_result.json)。
UTC 2026-09-09T20:15:52.472813+00:00 ～ 2026-09-09T20:15:58.235618+00:00、所要5.763秒。
2独立接続・7 SQL、専用database/role一致、別backend、session鍵の競合拒否と解放後取得、
両接続closeを確認。exit code 0だけでなくJSONの各assert成功を確認した。
実証明書検証・認証・SQL・排他は成功。業務テーブル作成/書込みはなし。
強制停止・本番容量・既存同期/outbox・大量負荷の試験ではない。

実行版は親コミットc046f0b＋証跡内の各ファイルSHA-256で固定。
SDK: psycopg/psycopg-binary 3.3.4、libpq 18.0、google-cloud-firestore 2.30.0、
google-api-core 2.36.0、certifi 2026.7.22。

Claude送信用の最終版（未送信）は `/Users/cnctor/.local/state/crm-capacity-trial-260910/claude-review-pending.txt`。
38,130文字、SHA-256 `910d1d66f889d2f049bea3a66795bec9362f82e0cd7058f749bd623ffef97ca0`。
コードの重複保管を避け、送信bundle自体はgitへ入れない。生成元はscripts/capacity_trialと専用テスト。

終了確認：専用試験Pythonプロセスは一覧照合で0件。Neon側は再読込したComputes画面で
ACTIVE（試験終了約2分後）、0.25 CU。5分無操作の自動休止設定は確認済みだが、
この記録時点で休止への到達は未確認。古い画面のSUSPENDED表示を終了証跡には使わない。
台帳予約累計：145秒、接続5本、SQL24本。失敗分を含む安全側の予約で、実使用数ではない。
