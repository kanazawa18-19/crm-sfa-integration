# 同期容量の専用資源と隔離試験記録

2026-09-10 主機CX。着手基準 `c046f0b`、製品基準 `d1b4060`。
本人「いけ」により、準備票の専用GCP・Neon作成と14日・合計20 USD管理予算を承認。
本番配備・監視登録・実Bot送信・資源撤去は対象外。

## 現在の到達点

**隔離試験全体は未完了。本人指定の `actionpoint-autocalc` に専用Firestore DBを作成済み、IAM付与の承認審査で停止。**
専用NeonはFreeで作成済み、本番アプリとの接続は0件。
本人「claude送信OK」後、Claude Opus 5・中の回答を取得し、必要な改善を反映。
接続先変更時に元の期限・使用量を保つ移行機能を追加。実Firestore文書操作はまだ0件。
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
| SA / custom role | capacity-trial-runner / capacity-trial-observer と各最小custom roleを新規作成。IAM付与なし |
| index / 文書 | 未作成・未書込み。専用SAの認証発行も未実施 |
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
DB作成応答は `freeTier:true`。最初に作ったDBの無料枠表示を確認した事実であり、大量試験の無料保証ではない。無料枠を引かず見積もり、請求実測の取得や費用停止機能の実サービス確認は未実施。

## GCPの再開条件

本人の「gcpはactionpoint-autocalcを使って」で接続先を変更。専用project新規作成待ちは解消。
既存用途のサービスアカウント2件は変更せず、専用named DBと専用SAを追加した。
API有効化は一度自動承認レビューに拒否されたが、先の14日/20 USD承認と接続先指定を
明記した再審査が通り実行済み。DB作成・SA2件・custom role2件も成功。
[資源読み戻し証跡](sync_capacity_gcp_resource_result.json)：専用SAを含むproject直接付与0、各SA上のbindingも0。継承を含む実効権限全体の否定ではない。

**IAM付与は2回とも自動承認レビューが拒否し、未実行。**
理由は「準備文書だけでは特定SAへの権限・対象・期限の明示承認を満たさない」。
本人の認証で直接文書を操作する代替は、専用SAの隔離検証を省くため採用しない。
次の権限案について本人の明示指示が必要：

| 対象 | 権限と範囲 |
|---|---|
| `capacity-trial-runner@actionpoint-autocalc.iam.gserviceaccount.com` | `capacityTrialRunner260910`：datastore.entities の get/list/create/update。専用DBだけ |
| `capacity-trial-observer@actionpoint-autocalc.iam.gserviceaccount.com` | `capacityTrialObserver260910`：get/list。専用DBだけ |
| `kanazawa@cnctor.jp` | 上記2 SAの短期token発行だけ（iam.serviceAccounts.getAccessToken）。project全体の代理権限は付与しない |

DB権限の条件は `resource.name=="projects/actionpoint-autocalc/databases/crm-capacity-trial-260910"`。
全付与の有効期限は `request.time < timestamp("2026-09-23T19:57:00Z")`（JST 9/24 04:57）。
削除・DB管理・既存アプリへの権限を含めない。token発行用のcustom roleと付与は未作成。
本人承認後、付与を読み戻し、専用SAによる許可/拒否の実地確認から再開する。

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

Firestore実IAM拒否・index query・保存/再起動・12process×3枠×5回・
5操作commit応答喪失・強制停止復旧・9回全件観測・持続負荷・監視合成受信口・別DB復元、
既存同期/outboxと実Neonの組合せは未検証。
新runnerにまだ実装していない試験はREADMEに明記する。
本番容量保証・本番7日観測も未達。NeonだけのSELECT/排他試験で代替しない。

## レビューとローカル検証

独立SEC最終BLOCKER 0 / WARN 0、品質最終BLOCKER 0 / WARN 0 / INFO 1。
旧版21772c6のQAは `tests/sync_capacity` 115成功・10skip（emulator未起動）、専用ガード37件はその内数。
今回の最終版は132成功・10skip（専用54件は内数）。独立SEC/品質の再レビューB0/W0。QAの拒否テスト改善も反映済み。
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
