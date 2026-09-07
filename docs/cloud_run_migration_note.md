# Python同期エンジンを Cloud Run へ移す

作成: 2026-09-07（主機CC）／状態: **第1段完了。Cloud RunからNeonへの到達確認済み**
／レビュー: 2026-09-07 に動物チーム3体が2周点検し、指摘を反映済み

## 1行で

Vercelの関数には「5分で強制終了」「定時実行が1時間ずれる」「ログが1時間で消える」の
3つの壁があり、同期エンジンがそこに当たっている。**Cloud Run はこの3つがまとめて外れる。**

## なぜ移すのか

| 困っていること | Vercel（今） | Cloud Run（移した後） |
|---|---|---|
| 長いバッチが途中で死ぬ | `maxDuration` **300秒固定** | リクエスト上限 **60分** |
| 定時実行がずれる | Hobbyのcronは**最大1時間のゆらぎ** | Cloud Scheduler は**分単位で正確** |
| 後から原因を追えない | ログ保持 **約1時間** | Cloud Logging に **既定30日** |

```
   今                                    移した後
   ─────────────────────────────         ─────────────────────────────
   バックフィルを分割して回避             分割せずそのまま流せる
   「cronが動いていない」の判断が困難      実行時刻がログに残る
   落ちた理由が翌朝には消えている          30日さかのぼれる
```

## 何を移して、何を残すか

**Webhookの受け口はVercelに残す。** kintone・Zoho・Notion の管理画面に登録済みのURLを
書き換えると、3サービスぶんの再登録と、その間の取りこぼしが発生するため。

```
   kintone / Zoho / Notion
        │  （登録済みURLは変えない）
        ▼
   ┌──────────────────────────┐
   │ Vercel                   │  受け口だけ残す。中身は Cloud Run へ中継
   │  api/index.py            │
   │  dashboard/（Next.js）    │  画面はVercelのまま
   └──────────────────────────┘
        │
        ▼
   ┌──────────────────────────┐
   │ Cloud Run                │  ★ここが移す先
   │  src/api/app.py（FastAPI）│  長いバッチ・cron・重い同期
   └──────────────────────────┘
        │
        ▼
   Neon Postgres（★据え置き。Supabaseへは移さない）
```

**DBはNeonのまま。** Supabaseへの移行は「これから新しく作るもの」からにする、と決めた
（2026-09-07）。稼働中の2システムを止める理由が今は無い。

**実装は二重に持たない。** `api/index.py`（Vercel）と Dockerfile（Cloud Run）は
どちらも同じ `src.api.app:app` を指している。コードは1つ。

## 段階

```
   第1段  読み取り1本だけ動かす      ✅ 完了（デプロイ・Neon到達確認済み）
          Cloud Runを並走させ、GCPからNeonへ届くかだけを確かめる
          Vercelは一切触らない
            │
            ▼
   第2段  cronをCloud Schedulerへ移す
          vercel.json の crons 9本を1本ずつ移す。移した順にVercel側を止める
            │
            ▼
   第3段  重いバッチをCloud Runへ寄せる
          バックフィル・reconcile。300秒の分割対応をここで畳む
            │
            ▼
   第4段  Webhookを中継にする
          Vercelの受け口は「受けてCloud Runへ渡すだけ」に痩せさせる
```

## 先にやること（1回だけ・本人の操作が要る）

```
   ① gcloud auth login          ← ブラウザが開く。会社アカウントで入る
   ② gcloud config set project <プロジェクトID>
```

**★ <プロジェクトID> をどうするか（まだ決まっていない）**

```
   ┌─ 既に会社のGCPプロジェクトがある ──▶ そのIDを使う
   │                                      gcloud projects list で一覧が出る
   └─ 無い ──────────────────────────▶ 新しく作る
                                          gcloud projects create crm-sfa-integration
                                          そのあと GCPコンソールで「お支払い」を紐づける
```

**課金の紐づけが要る。** Cloud Run も Cloud Build も、課金アカウントが無いプロジェクトでは
API を有効化できない。無料枠はあるが、枠の中で使うにも課金アカウントの登録自体は必要。

**gcloud 自体は入れてある**（2026-09-07、`~/google-cloud-sdk`、v583.0.0）。
Homebrew が macOS 26 に未対応で壊れているため、公式tarballを `$HOME` 直下へ展開し、
`~/.zshrc` から PATH を通してある。新しいターミナルを開けば `gcloud` が使える。

**docker は要らない。** `gcloud run deploy --source .` はイメージを Cloud Build 側で
作るので、手元にDockerを入れる必要はない。

## 決まったこと（2026-09-07 夕方）

```
   GCPプロジェクト   fabled-electron-406310
                    表示名は crm-sfa-integration-cloudrun に変えてある
   リージョン        us-east4（北バージニア）
                    ★ 2026-09-07 に asia-northeast1（東京）から変更。
                      Neon が AWS us-east-1（北バージニア）にあり、東京だと
                      DBアクセスのたびに太平洋を往復する（片道100〜150ms）
   サービス名        crm-sfa-backend
   課金アカウント     01EA6F-556121-34B9A6（紐づけ済み）
```

**★ 新規プロジェクトは作れなかった。** プロジェクト数が上限の30個に達していたため。
削除はせず、**空だった既定プロジェクト（旧名 `My First Project`）を再利用した**。
Cloud Run / Secret Manager / Compute のAPIが一度も有効化されておらず、中身が無いことを
確認済み。APIを足すだけなので、他のプロジェクトやGASには影響しない。

**シークレットの値の出どころ**（Vercelからは読み戻せないので手元のファイルから取った）

| シークレット | 出どころ |
|---|---|
| `DATABASE_URL` | `dashboard/.env.local`（Neon pooled） |
| `DATABASE_URL_UNPOOLED` | `dashboard/.env.local`（Neon 非pooled） |
| `DASHBOARD_API_TOKEN` | `config/.env` |

**★ `DASHBOARD_API_TOKEN` はローカルの値。本番と同じとは限らない。**
第1段は「コンテナと `smoke_test.sh` が同じ値を見る」だけで成立するので支障は無いが、
第2段以降で本番と揃える必要が出たら差し替えること。

**★ Auto Mode は `gcloud run deploy` をブロックする**（`vercel --prod` と同じ扱い）。
API有効化・シークレット登録・プロジェクト操作は通る。**止まるのはデプロイだけ**なので、
そこだけ本人が `!` を付けて叩く。

## 第1段でやること（3コマンド）

```
   ①  pbpaste | bash scripts/cloud_run/bootstrap_secrets.sh DATABASE_URL
       pbpaste | bash scripts/cloud_run/bootstrap_secrets.sh DATABASE_URL_UNPOOLED
       pbpaste | bash scripts/cloud_run/bootstrap_secrets.sh DASHBOARD_API_TOKEN
       pbpaste | bash scripts/cloud_run/bootstrap_secrets.sh CRON_SECRET
                     ▲ 先頭3つは登録済み。CRON_SECRETだけ第2段のデプロイ前に登録する

   ②  bash scripts/cloud_run/deploy.sh --dry-run   ← 何が起きるか見る（何もしない）
       bash scripts/cloud_run/deploy.sh            ← 実行。yes と打つまで止まる

   ③  bash scripts/cloud_run/smoke_test.sh
```

先頭3つは2026-09-07に登録済み。`CRON_SECRET`は第2段で追加するため未登録。
現在の`deploy.sh`は4つすべてを要求するので、次の再デプロイ前に登録する。

③で `"ok": ["postgres", "advisory_lock"]` が返れば第1段は合格。
**確かめたいのは1点だけ：GCPからNeonへ、pooled と 非pooled の両方で届くか。**
非pooled（`DATABASE_URL_UNPOOLED`）は排他制御に使っている接続で、ここが届かないと
夜間バッチの二重実行を防げない。

### ★ Vercelの値は読み戻せない

Vercel の Sensitive 指定の環境変数は、画面もCLIもプレースホルダしか返さない。
**Neonのダッシュボードなど、発行元から取り直すこと。** Vercelからコピーはできない。

## 置いたファイル

| ファイル | 何をするもの |
|---|---|
| `Dockerfile` | FastAPIをコンテナにする。Vercelと同じPython 3.12 |
| `.dockerignore` | イメージに入れないもの（tests/ dashboard/ 等） |
| `.gcloudignore` | Cloud Buildへアップロードしないもの。`.dockerignore` とは別物 |
| `requirements.txt` | **本番用だけ**に絞った（pytest等を外した） |
| `requirements-dev.txt` | ローカル・CI用。`-r requirements.txt` ＋ テスト依存 |
| `scripts/cloud_run/config.sh` | プロジェクトID・リージョン・サービス名 |
| `scripts/cloud_run/bootstrap_secrets.sh` | Secret Managerへ認証情報を登録 |
| `scripts/cloud_run/deploy.sh` | API有効化 → SA作成 → 権限付与 → デプロイ |
| `scripts/cloud_run/smoke_test.sh` | `/openapi.json` の起動確認と DB到達の確認 |

`requirements.txt` を分けたので、**CI（`.github/workflows/ci.yml`）は
`requirements-dev.txt` を入れるように直してある。**

## 引っかかるところ

### ★ Cloud Run IAMとアプリ認証のヘッダー競合

#### 手動のAPI呼び出し

Cloud RunのIAM認証も、このアプリのトークン認証も、どちらも `Authorization: Bearer ...`
を使うのでぶつかる。Googleの仕様では**両方あるときは `X-Serverless-Authorization`
だけを検証し、`Authorization` はそのままコンテナへ渡す**と決まっている。

```
   X-Serverless-Authorization: Bearer <GoogleのIDトークン>   ← Cloud Runが見る
   Authorization:              Bearer <DASHBOARD_API_TOKEN>  ← アプリが見る
```

GoogleのIDトークンは、IAM Credentials APIで実行用サービスアカウントのものを発行し、
Cloud RunのURLを発行先（audience）に明示する。本人アカウントで単に
`gcloud auth print-identity-token` を実行すると、発行先がgcloud自身のクライアントIDになり、
この環境ではCloud Runの手前で404になる。また、gcloudのサービスアカウント偽装は
アクセストークン発行権限まで要求するため使わない。`deploy.sh` は次の最小権限を設定する。

- 実行用サービスアカウント自身: 対象サービスの呼び出し権限（`roles/run.invoker`）
- デプロイした本人: 実行用サービスアカウントのトークン発行権限
  （`roles/iam.serviceAccountOpenIdTokenCreator`。IDトークンの発行だけに限定）

出典: https://docs.cloud.google.com/run/docs/authenticating/service-to-service

#### Cloud Schedulerからの呼び出し

**第2段の方式は2026-09-07に確定した。** Cloud Scheduler は OIDC トークンを
`Authorization` に載せ、アプリ側の `CRON_SECRET` はカスタムヘッダー
`X-Cron-Secret` に載せる。`verify_cron_secret`は次の両方を受け付けるため、1本ずつ移しても
既存のVercel Cronを壊さない。

```
   移行前  Authorization: Bearer <CRON_SECRET>  ← Vercel Cron
   移行後  Authorization: Bearer <Google OIDC>  ← Cloud Run IAM
           X-Cron-Secret: <CRON_SECRET>          ← アプリ
```

Cloud SchedulerはOIDCを有効にすると、カスタム設定した`Authorization`を上書きする。
一方、任意のカスタムヘッダーは送れるため、専用ヘッダーへの分離が必要になる。
出典: https://docs.cloud.google.com/scheduler/docs/reference/rest/v1/projects.locations.jobs

`CRON_SECRET`を廃止してCloud Run IAMだけに寄せる案は見送った。同じアプリをVercelでも
動かす段階移行中であり、アプリ自身の認証を残した方が誤設定時にも二重の歯止めになるため。

第2段のデプロイ前に、Vercelと同じ`CRON_SECRET`をSecret Managerへ登録する。
**現在の`deploy.sh`は第2段対応版なので、この登録後でなければ再デプロイできない。**

```
   pbpaste | bash scripts/cloud_run/bootstrap_secrets.sh CRON_SECRET
```

Cloud Schedulerのジョブ設定には`X-Cron-Secret`の値が保存される。閲覧権限を持つ人からも
隠す保管庫（Secret Manager）ではないため、SchedulerのIAM閲覧権限は必要最小限にする。
値をコマンドラインへ直接書くとシェル履歴や`ps`に残るので、ジョブ作成スクリプトでは
標準入力または一時ファイル経由で渡す。

### 書き込めるのは /tmp だけ

Cloud Run のファイルシステムは読み取り専用（`/tmp` を除く）。
`SYNC_ID_MAPPING_BACKEND` を Postgres にしている限り問題ないが、
sqliteバックエンド（`SYNC_ID_MAPPING_DB_PATH`）に落とすと書き込みで落ちる。

### 環境変数が48個以上ある

`src/` が読む環境変数は実測で48個（定数経由のものを含めるともう少し多い）。
第1段は3つで足りるが、**第3段までに残りをSecret Managerへ移す必要がある。**
一度に全部やらず、機能を移すたびに必要なものだけ足す。

## 動いた後、ログはどこで見るか

移行の動機の1つが「Vercelのログが1時間で消える」ことなので、ここが本題。

```
   コマンドで見る（早い）
     gcloud run services logs read crm-sfa-backend \
       --region=us-east4 --limit=50

   画面で見る（絞り込み・期間指定ができる）
     https://console.cloud.google.com/logs
     → リソース「Cloud Run リビジョン」で絞る
```

**既定で30日残る。** Vercel の1時間とはここが違う。

## 費用と、試してやめるときの片付け

```
   待機中          ほぼ無料（--min-instances=0 なので、呼ばれなければ動かない）
   呼ばれたとき     リクエスト数と実行時間に応じた従量。第1段の使い方なら月数十円の規模
   ビルド          Cloud Build の無料枠（1日120分）に収まる見込み
   イメージの保管   Artifact Registry。数百MBで月数十円
```

**やめるときは3つ消す。**

```
   gcloud run services delete crm-sfa-backend --region=us-east4
   gcloud artifacts repositories delete cloud-run-source-deploy --location=us-east4
   gcloud secrets delete DATABASE_URL          # 他2つも同様
```

## 注意していること

### ★ デプロイされるのはアプリ全体（診断1本ではない）

第1段で確かめたいのは読み取りの診断エンドポイント1本だが、コンテナに載るのは
`src.api.app:app` **全部**。Webhook受信・cron・書類生成・一括メール送信も含まれる
（実装を二重化しない方針の必然）。

**歯止めは IAM 認証だけ。** `--no-allow-unauthenticated` で、`run.invoker` 権限を
持つ主体以外は呼べない。`deploy.sh` が付けるのは、疎通確認で使う
**実行用サービスアカウント自身へのサービス単位の権限だけ**。本人はそのIDトークンを
発行して呼び出す。一般ユーザーや外部サービスには `run.invoker` を付けない。
**将来この権限を誰かに渡すときは、「読み取りのつもりが書き込み系にも届く」ことを思い出すこと。**

### ★ Neon 側のIP制限

GCPから Neon へ届くかは、Neon の Allowed IPs 設定次第。
Cloud Run の送信IPは固定ではないため、Neon側でIP制限をかけていると届かない。
`smoke_test.sh` が `failed` を返したら、まずここを疑う。

### ★ 同時実行の設定は第1段向けの値

`--concurrency=20 --max-instances=2`（最大40同時）は、軽い診断エンドポイント用に選んだ値。
第3段で重いバッチを載せるときは見直す。**排他制御用の非pooled接続は
Vercel 側も並行して使っている**ため、両方合わせて Neon の接続枠を食い合う。

## 2026-09-07 のレビューで直したこと

| 直した所 | 何が問題だったか |
|---|---|
| `.vercelignore` | `.env*` と `migration_output/`（PII）を除外していなかった。**`.vercelignore` があると Vercel は `.gitignore` を見ない**ので、これまで認証情報とPIIがアップロード対象だった。今回まとめて塞いだ |
| `deploy.sh` | 「確認を求める」とコメントにあるのに、実際は止まらず課金対象の操作まで走り切っていた。`yes` と打つまで止まるようにした。失敗時に Cloud Build のログの見方も出す |
| `smoke_test.sh` | `set -e` のせいで、**失敗したときだけ「見るところ」の説明に到達しない**逆転が起きていた。失敗時こそ原因の当たりを日本語で出すようにした。トークンを `ps` から見えない渡し方（プロセス置換）に変えた |
| `bootstrap_secrets.sh` | 「画面には出ません」が**嘘**だった（`--data-file=-` はエコーを止めない）。`read -rs` でエコーを止め、`pbpaste` 経由を推奨に。空のまま登録できてしまう穴も塞いだ |
| `requirements.txt` | `boto3` / `python-dotenv` が**どこからも import されていなかった**（全文検索で0件）。外した。`httpx` は `fastapi.testclient` 専用なので `requirements-dev.txt` へ移した |
| `.github/workflows/ci.yml` | Dockerイメージのビルドを CI に足した。**無料の場所で先に転ばせる**ため。ビルド後に起動して `/healthz` が返るところまで見る |

## 確かめたこと / 確かめていないこと

| | 状態 |
|---|---|
| `uvicorn src.api.app:app` で起動し `/healthz` が200 | ✅ ローカルで実測（2026-09-07） |
| 認証なしで診断が401、トークンありで200 | ✅ ローカルで実測 |
| テスト2,633件 | ✅ 全部通る |
| `requirements-dev.txt` が解決できる | ✅ `pip install --dry-run` で確認 |
| `deploy.sh --dry-run` が正しいコマンドを組み立てる | ✅ 実測（2026-09-07） |
| gcloud の導入（`~/google-cloud-sdk` v583.0.0） | ✅ 実測。`gcloud version` が通る |
| 本番用依存だけで `src.api.app` が import できる | ✅ クリーンなvenvで実測（2026-09-07・レビュー時） |
| `/healthz` が200・トークン無しの診断が401 | ✅ Dockerfileと同じCMDで再現して実測 |
| テスト2,633件（本番用依存を削った後） | ✅ クリーンなvenvで再実行し全部通る |
| **Dockerイメージのビルド** | ⚠️ ローカルにdockerが無く未実行。**CIに `docker build` を足したので、push すれば無料で分かる** |
| **Cloud Runへのデプロイ** | ✅ 旧リージョンで実測。コンテナ起動・リビジョンReadyを確認（2026-09-07） |
| **Cloud Runの公開経路** | ✅ 同じプロジェクト・`us-east4` の公式helloでHTTP 200と到達ログを確認（2026-09-07） |
| **非公開crm-sfaへの正しいIDトークン認証** | ✅ IAM Credentials API＋限定権限で実測（2026-09-07） |
| **GCPからNeonへの到達** | ✅ pooled通常接続・非pooled排他制御接続とも実測（2026-09-07） |

`/healthz` はローカルでは200だが、このCloud RunサービスではGoogle FrontendのHTML 404となり、
コンテナ到達ログも無い。原因は未確定。一方、同じサービスの `/openapi.json` と
`/api/diagnostics/integrations` は200でコンテナまで届くため、第1段の**起動確認**には
`/openapi.json` を使う。これは `/healthz` 自体の健康確認を代替するものではない。

**`Dockerfile` は書いただけで、手元では一度もビルドしていない**（ローカルに docker が無い）。
ただし2026-09-07のレビューで CI に `docker build` ＋ 起動確認を足したので、
**push した時点で GitHub Actions が無料でビルドを試す。** そこが緑なら、
Cloud Build 上での初ビルドが転ぶ確率はかなり下がる。
