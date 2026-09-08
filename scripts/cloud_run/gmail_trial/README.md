# Gmail通知結果ログの隔離検証

このサービスは合成ログだけを作る。Gmail、業務DB、Secret Managerには接続しない。
製品の結果ログ2ファイルを無変更で使用する。製品の期限更新処理の実機検証を置き換えない。

- `POST /probe/success`: 成功1件、HTTP 200。
- `POST /probe/partial_failure`: 成功1件・失敗1件、HTTP 200。
- `POST /probe/http500`: 成功1件（status=success）の終了記録後にHTTP 500。HTTP失敗と業務結果は別に判定する。
- `POST /probe/unfinished`: 開始・進捗だけを出す。強制終了の実再現ではない。
- `GET /api/healthz`: 起動確認。
- `GET /check`: Cloud Loggingの開始・終了を実行UUIDで突合する。業務処理は再実行しない。

必須環境変数は `GCP_PROJECT_ID` と `TRIAL_SERVICE_NAME=crm-gmail-watch-trial`。
`GRACE_SECONDS=300`、`LOOKBACK_SECONDS=86400` が既定。猶予300秒はログ取り込み遅延を吸収する想定値であり、実際の到達時間を測って妥当性を確認する。猶予は正数かつ検索期間未満、検索期間は最大7日。
検索は検証サービスのみに限定し、全ページ成功後に判定する（最大100ページ・合計20,000件）。
単調増加時計で40秒の予算を各リクエストの前後に確認し、通信timeoutは20秒と残時間の小さい方を使う。
urllibのtimeoutは厳密な処理全体の停止期限ではなく、少量の応答が継続すると40秒を超える可能性がある。
保証するのは要求前後の期限確認と、超過した結果を成功として採用しないこと。厳密な強制停止は未検証。
予算超過は結果を部分的に採用せず `monitor_error` にする。Cloud Runのtimeoutは60秒以上とする。
不正データや検索失敗は `monitor_error` とHTTP 500。固定stageでconfiguration（設定）・retrieval（取得）・matching（突合）を区別し、例外本文は出さない。検索成功は `check_completed`。
HTTP本文にも `entry_count` を返す。0件は照合処理が成功していてもログ到達は未確認。
端末間の時計差は開始時刻の未来60秒まで許容し、その間は未終了判定しない。
未来61秒以上と、開始時刻より記録時刻が前のログは不正データとして扱う。
猶予を過ぎた開始に同じIDの終了がなければ `missing_finish`。結果不明であり、未実行の証拠ではない。

Cloud Runは未認証公開しない。検証サービスのログだけを固定条件のsinkでglobalの専用bucket `crm-gmail-watch-trial` へ送る。
専用サービスアカウントにはその `_AllLogs` viewだけの `roles/logging.viewAccessor` を付ける。プロジェクト全体のログ閲覧権限は付けない。
検索先も `projects/{project}/locations/global/buckets/crm-gmail-watch-trial/views/_AllLogs` に固定する。
Cloud SchedulerからOIDCで `/check` を定期実行する。
ログアラートは検証サービスに絞り、partial_failure/failed、missing_finish、monitor_errorを監視する。
HTTP 500はCloud Runリクエストログでも監視する。通知先と通知到達は別途確認する。

## 制約

- 検索期間より古い未終了は検索から消える。永続的な未終了台帳ではない。
- ログの到着遅延で一時的な結果不明が出る。後着終了は次回の照合で解消する。
- 通知抑制はCloud Monitoringログアラートの通知間隔に任せる。永続的な重複排除は保証しない。
- 開始ログ自体がない実行は検出できない。Schedulerの実行失敗と監視自身の停止は別途監視する。
- `check_completed` 自体の欠落も期間を決めて監視する。Cloud Runの強制終了では `monitor_error` を出せない場合がある。
- アラートの自動クローズは、後着終了との突合による業務成功を意味しない。

ビルドにはリポジトリ全体を渡さず、Dockerfile、requirements、app、製品の上記2ファイルだけを
同じ相対パスで隔離ディレクトリにコピーし、このDockerfileを指定する。認証情報を含めない。


## 監視設定の保存用テンプレート

- `alert_policies.json`: 業務結果の失敗・HTTP 500・終了記録欠落・監視エラー・到達未確認（照合0件）の5条件。
- `check_metric.json`: `check_completed`件数を数えるログ指標 `crm-gmail-trial-checks`。
- `check_absence_policy.json`: 上記指標が600秒欠落した場合の条件。

全ポリシーは `enabled=false`、`notificationChannels=[]` の未稼働テンプレート。
実登録時に検証用の通知チャネルを必ず指定し、有効化後に通知到達を確認する。
テンプレートを保存しただけでは監視完了ではない。
欠落条件は10分間隔で合計し（ALIGN_SUM）、さらにrevision・instanceを分けず合計する（REDUCE_SUM）。
これにより古いrevisionの時系列だけが止まったことによる誤報を避ける。

[Google公式の欠落監視手順](https://docs.cloud.google.com/monitoring/alerts/metric-absence)のとおり、
ポリシー作成・変更後に実データが一度も届いていない場合、欠落条件では検出できない。
指標への初回到達を確認した後、検証用監視の停止・復旧によって通知と回復を実検証する必要がある。
10分の集計窓に加え、ログ到達・指標反映・条件評価・通知には遅延があるため、停止からちょうど10分での通知は保証しない。


## 検証時の順序と終了

1. ログ指標を登録する。
2. `/check`成功と `check_completed` の初回指標到達を確認する。ログ0件なら到達未確認を解消する。
3. 通知先を設定して欠落アラートを有効化する。有効化直後にも `/check` を成功させ、指標到達を確認する。ポリシー作成・変更後のデータが必要なため、この確認を省略しない。
4. 有効化後の指標到達を確認してから、専用監視の停止・復旧で通知到達と回復を確認する。初回到達前から完全に動いていない状態は、欠落条件では検出を保証しない。

定期チェックは専用Scheduler `crm-gmail-watch-trial-check` だけを使用し、毎分GET `/check`。
認証は専用サービスアカウントのOIDCで、audienceはCloud Runサービスのbase URL（`/check`を付けない）。
本番Schedulerを変更しない。
試験終了後はこの専用Schedulerを停止し、検証アラートを無効化する。これで合成unfinishedの繰り返し通知を止める。
停止しても進行中の処理を取り消したことにはならない。

ログは専用bucketのほか `_Default` にも残り得る。将来の検索・指標・アラートでも検証のservice_name絞り込みを必須にする。
本番sinkは変更しない。
`monitor_error`は固定のerror_kind（http/timeout/invalid/other）を記録する。
HTTPエラーでは100〜599の整数コードだけを追加し、例外本文・型名・URL・認証情報は記録しない。

Dockerビルド時に `import app` を検査し、実行は非root（UID/GID 65532）とする。
検証専用のため、ベースイメージのdigestと推移依存の完全固定は未対応。同一内容の再ビルドでも依存が変わる可能性がある。

通知タイミングはログ条件5件を `notificationPrompts=["OPENED"]`、照合完了欠落条件を `["OPENED", "CLOSED"]` に明示する。欠落条件では停止検出と復旧の両方の通知を要求する。これは通知設定の明示化であり、実機の未発報・メール未到達の原因が確定したことを意味しない。通知到達は別途実測する。
