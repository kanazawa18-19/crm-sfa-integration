# 同期容量の実サービス確認（2026-09-10 主機CX）

対象コード `7ff3545`。管理画面・GET・既存ログの読み取り。初回記録 `ddef9d3` 後、
本人承認を受けCLI認証を復旧し、Firestore一覧・IAM・監査・7日分ログを追加取得した。
**本番容量保証は未充足。今回の成果は、現行設定と配備前に不足する条件の特定。**
実同期・drain・負荷試験・Slack投稿・配備・DB作成はしていない。
ただしFirestore画面の閲覧に伴い、APIの有効化が03:49:16 JSTに完了した。
監査ログで照合済みであり、「設定変更が一切なかった」とは報告できない。

```text
要求受付 → Firestore保管 → 定時worker → 同期完了
             DBなし         未登録       本番未検証
                   ↓
             滞留・失敗・処理時間の監視も未配備
```

## 実設定の確認結果

| 対象 | 確認値 | 判断・限界 |
|---|---|---|
| Vercel本番 | `dpl_5iwWWdhTbz56vhGZtuvC7gurZT4g` READY | 前回と同じ配備。新しい容量制御の実績ではない |
| Vercel実行設定 | Fluid有効、iad1 | DB枠に対応する全体同時処理上限・タイムアウト値は今回取得不能 |
| Vercel定時登録 | 9本、disabledAt=null。outboxは `0 17 * * *` | sync-capacity-drainなし。登録は送達・業務成功の証明ではない |
| Vercel環境変数名 | 取得結果の `SYNC_CAPACITY_` 接頭辞は0件 | 秘密値は表示・保存せず。取得結果の範囲の判定 |
| Cloud Run稼働版 | `crm-sfa-backend-00003-rv8`へ100%、他2版0% | 対象プロジェクト `fabled-electron-406310`、us-east4 |
| Cloud Run容量 | サービス最小0/最大20、リビジョン最小0/最大2、各最大同時20 | サービス上限とリビジョン上限を混同しない。2×20をDB安全容量の保証にしない |
| Cloud Run実行 | タイムアウト3,600秒、CPU1、メモリ1GiB、リクエストベース課金 | 待機列workerの実処理時間は未検証 |
| Cloud Run環境変数名 | 6項目、容量設定なし | 有効フラグ1つとSecret参照5つ。秘密値は取得しない |
| Cloud Scheduler | 2本。暗号鍵診断Enabled、Gmail検証Paused | 対象プロジェクト一覧にsync-capacity-drainなし |
| Cloud Monitoring | ポリシー6件、すべてGmail検証用・オフ | 同期容量用の有効な監視はこのプロジェクトにない。外部全サービスの監視不在を証明したものではない |
| Neon | Free、0.25 CU・約1GB RAM、5分自動休止、main | 現在の容量を確認。SQL再実行はしていない |
| Neon接続ピーク | 過去1日のPostgres connections countで `Some metrics are unavailable.` | ピークを取得できず。9/9 SQL実測の通常枠106本は過去値のまま |
| Firestore API | CLIでENABLED、有効化成功の監査ログあり | DBの利用準備完了を意味しない |
| Firestore DB | CLI一覧取得が成功し `[]`、0件 | 対象プロジェクトに保存先DBなし。別プロジェクトの有無は未調査 |
| Firestore権限 | プロジェクトIAMのFirestore/Datastore名の役割0件、Cloud Run実行SAへの直接割当0件 | 継承・グループ・カスタム役割まで含めた実効権限の証明ではない |
| Firestore index/TTL/保存・復元 | 検証対象DBなし | 専用DBと権限の準備後に隔離検証が必要 |

Vercelは `.vercel/project.json` の対象にGET `/v9/projects/{id}`。
Cloud Runは変更履歴・スケーリング画面、Scheduler/Monitoringは対象プロジェクトの一覧。
Neonは `morning-voice-67655784` / `br-raspy-hall-awjghosc` / `ep-billowing-art-awbkegin`。
表示名や遅延のあるグラフを、別環境や別期間の実測へ転用していない。

## 流量と所要時間で確認できた範囲

| 観測 | 取得結果 | 容量見積りに使えるか |
|---|---|---|
| Vercel production、直近24hログ・requestPath集計 | gmail-push 2、email-reminder-check 1、document-approval-poll 1。3経路4件 | 対象4Webhookは含まれない。保持範囲・ログ件数の制約があるので全流入0件とはしない |
| Vercel同期間、Webhook文字列・HTTP状態集計 | 200が2件 | Gmail経路を同期4Webhookの性能へ転用しない |
| Cloud Run 9/9 10:00:00.793 JSTのログ | 暗号鍵診断、Google-Cloud-Scheduler、GET200、6.692秒 | 実際の診断1件のHTTP所要時間。同期時間ではない |
| 同Scheduler一覧 | 前回9/9 10:00:00成功、次回9/10 10:00:01 | 定時の起動とCloud RunへのHTTP到達を照合。本文の業務結果は未取得 |
| Cloud Run直近1日グラフ | 9/9 03:48:16〜9/10 03:48:16 JST。リクエストレイテンシ125.141〜130.981ms、end-to-end 6.853〜7.173秒 | 分位・集約されたグラフの表示範囲。生ログの最小/最大・同期worker時間とは扱わない |

新しい待機列は1callで最大1job。毎分1callなら理論上1,440job/日だが、
実行時間・満枠・初期化retry・呼出し欠落で低下する。今回は実処理可能件数を測定していない。
Webhook＋outbox＋retryの総流入量、ピーク1分/5分、業務別の処理時間、
積み残しを減らせる余力は依然として未確定。

### 認証復旧後のCloud Runリクエストログ集計

対象サービス `crm-sfa-backend`、`run.googleapis.com/requests`。
期間は9/3 04:00以上〜9/10 04:00未満JST（UTC 9/2 19:00〜9/9 19:00）。
取得上限10,001に対し35件で打切りなし。URLのqueryや本文は出力せずpath単位に集計。

| 経路 | 件数 | HTTP状態の内訳 |
|---|---:|---|
| `/` | 5 | 403:2、404:3 |
| `/api/cron/daily-batch` | 1 | 401:1 |
| `/api/cron/token-encryption-healthcheck` | 4 | 200:3、401:1 |
| `/api/diagnostics/integrations` | 8 | 200:8 |
| `/api/health` | 2 | 403:1、404:1 |
| `/api/healthz` | 1 | 200:1 |
| `/docs` | 2 | 200:2 |
| `/healthz/` | 1 | 307:1 |
| `/healthzz` | 1 | 404:1 |
| `/nonexistent-abc` | 1 | 404:1 |
| `/openapi.json` | 9 | 200:9 |
| **合計** | **35** | **200:23、307:1、401:2、403:3、404:6** |

対象4Webhook・outbox・sync-capacity-drainのリクエストログは0件。
このサービス・ログ・期間に限定した件数であり、Vercelや送信元を含む全流入0件ではない。
401/403/404は過去の記録で、今回の試運転で発生させたものではない。
診断8件は0.080661052〜0.898174492秒、暗号鍵診断の最長は6.692052924秒。
同期処理の標本がないため、これらで同期の枠数やworker頻度を決めない。

## Firestore画面の副作用とCLIでの確認完了

1. Google CLIは認証更新で失敗。ChromeのGoogleログイン画面へのアクセスは
   初回自動承認レビューに拒否されたが、本人「承認します」後に再開し、
   本人がパスキー確認を完了した。
2. Firestoreのデータベース一覧を開くと、「Firestore APIが有効ではなく、
   自動で有効にすることを試みた」というエラーと「有効なAPI: Firestore API」の通知が出た。
   有効化ボタン・DB作成ボタンは押していない。
3. 別の「有効なAPIとサービス」一覧でCloud Firestore API掲載を確認した。
   初回記録時は監査ログ未照合だったが、後述の追加確認で有効化成功を確定した。
4. Firestore一覧の再読み込みは、自動有効化再試行による永続設定変更を理由に
   自動承認レビューが拒否した。実行せず、回避もしない。
5. 安全な代替として `gcloud firestore databases list` を試したが、CLIの再認証要求で失敗。
   ブラウザの本人確認完了だけではCLI認証は復旧しなかった。
6. 本人「承認するで」後の一覧再表示も、APIの恒久有効化は明示承認外という理由で拒否。
   画面の再表示は行わず、承認済みのCLI再認証を開始した。パスワード入力方式は中止し、
   `gcloud auth login kanazawa@cnctor.jp --force --brief` の公式ブラウザ認証で成功終了。
   パスワード・認証コード・トークンは取得・記録していない。
7. CLIのDB一覧は終了コード0・0件。Service UsageはENABLED。監査は
   `google.api.serviceusage.v1.ServiceUsage.EnableService`、同一operationの開始
   03:49:14.643343 JST、完了03:49:16.419063743 JST、エラーなしを確認。
   principalは本人アカウント、User-AgentはChrome。複数表示された同一operationの
   記録を複数の有効化操作とは数えない。
8. Cloud Run実行SAは `crm-sfa-backend-sa@fabled-electron-406310.iam.gserviceaccount.com`。
   プロジェクトIAMへの直接割当と、役割名にFirestore/Datastoreを含む割当は各0件。
   Secretへの個別権限や上位組織の継承権限を否定する意味ではない。

Firestore資源を作成・削除していない。APIを無断で無効化して戻す操作もしていない。
DB0件はCLIで確定した。画面の拒否に対しては、設定変更を伴わないCLI読取りで
必要な確認を完了したため、画面再表示の追加承認は不要。

## 次の作業と合格条件

| 順番 | 作業 | 合格条件 |
|---|---|---|
| 1（確認済み） | CLI再認証・Firestore読み取り | 認証成功、DB0件・API有効化監査・プロジェクトIAMを取得。保存先は未準備と判定 |
| 2 | 本番の継続観測を準備 | 日数・対象経路を明記した総流入/ピーク/再送数と、Neon他用途を含む接続ピーク。ログ欠落は0件に置換しない |
| 3 | 隔離された実Firestore・DBで確認 | 専用権限、保存/復元、競合、応答喪失、所要時間、持続処理量と滞留解消を確認。実資源作成は別指示 |
| 4 | 同期用の定時drain・監視を準備 | 最古pending/retry、processing保持、needs_attention、最終completed、Scheduler途絶、処理時間、保存失敗の観測と通知。実有効化前に検証 |
| 5 | 容量値と切替手順を確定 | 他用途/余裕を控除したDB予算から枠を決め、実処理量が流入を超える。新旧worker混在と停止確認も解決 |

今回の読み取りから安全な枠数を仮置きしない。別イシューの実装や資源作成は開始せず、
本番配備は保留する。実Bot本人ID・権限・送達も未検証のまま。

関連：[待機列仕様](sync_capacity_queue.md)、[接続容量](sync_api_connection_capacity.md)、
[配備条件](sync_api_deployment_readiness.md)。
