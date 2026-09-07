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

## 第1段（完了済み）の再デプロイと、第2段の準備

```
   ①  pbpaste | bash scripts/cloud_run/bootstrap_secrets.sh DATABASE_URL
       pbpaste | bash scripts/cloud_run/bootstrap_secrets.sh DATABASE_URL_UNPOOLED
       pbpaste | bash scripts/cloud_run/bootstrap_secrets.sh DASHBOARD_API_TOKEN
       pbpaste | bash scripts/cloud_run/bootstrap_secrets.sh TOKEN_ENCRYPTION_KEY
       pbpaste | bash scripts/cloud_run/bootstrap_secrets.sh SLACK_WEBHOOK_URL_ALERT
                     ▲ 5つとも登録済み（前3つ 09-07、後2つ 09-08）。作業は不要

   ②  bash scripts/cloud_run/deploy.sh --dry-run   ← 何が起きるか見る（何もしない）
       bash scripts/cloud_run/deploy.sh            ← 実行。yes と打つまで止まる

   ③  bash scripts/cloud_run/smoke_test.sh
```

| 目的 | 必要なシークレット |
|---|---|
| 第1段のDB診断だけ | `DATABASE_URL` `DATABASE_URL_UNPOOLED` `DASHBOARD_API_TOKEN` |
| 第2段の1本目まで | 上の3つ＋`TOKEN_ENCRYPTION_KEY` `SLACK_WEBHOOK_URL_ALERT` |

**5つとも登録済み。追加の入力作業は無い。** `CRON_SECRET` は要らなくなった
（理由は第2段の節）。

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
| `scripts/cloud_run/manage_scheduler_job.sh` | cronを1本ずつ作成・試運転する。Vercel停止前の順序を固定する |
| `scripts/cloud_run/list_cron_env.py` | どのcronがどの環境変数を読むかを静的に棚卸しする |

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

Cloud SchedulerはCloud Run IAMのOIDC認証で呼出元を制限する。アプリ側はCloud Runにだけ
`CLOUD_RUN_SCHEDULER_AUTH_ENABLED=true`を置き、Schedulerが付ける
`X-Cloud-Scheduler: true`を受け付ける。Vercel環境にはこの設定を置かないため、同じヘッダーを
外部から偽装しても通らない。Vercel Cronは従来どおり`CRON_SECRET`を使い続ける。

```
   移行前  Authorization: Bearer <CRON_SECRET>  ← Vercel Cron
   移行後  Authorization: Bearer <Google OIDC>  ← Cloud Run IAM
           X-Cloud-Scheduler: true               ← Cloud Run側だけが受理
```

Cloud SchedulerはOIDCを有効にすると、`Authorization`をGoogleのIDトークンに使う。
出典: https://docs.cloud.google.com/scheduler/docs/reference/rest/v1/projects.locations.jobs

Cloud Schedulerのジョブ設定に秘密値を置かないため、閲覧権限から合言葉が漏れる経路もない。
Cloud Runへの到達はScheduler専用サービスアカウントの`run.invoker`権限で制限する。

### 第2段：cronを1本ずつ移す

**Vercelのcronを先に消してはいけない。** 移行対象を1本選び、Cloud Schedulerが実際に
200を返したことを確認してから、同じpathだけをVercelから止める。

**現在のCloud Runには第1段用の3つしか入っていない**（`crm-sfa-backend-00002-x7v`
を実測。`CLOUD_RUN_SCHEDULER_AUTH_ENABLED` も付いていないので、**今のリビジョンに
Schedulerから叩くと401になる**）。9本の移行はどれも再デプロイが前提。

#### 9本が読む環境変数（2026-09-08 実測）

各エンドポイントから到達するモジュールを辿って集めた「使いうる上限」。
分岐で実際は読まないものも含む。

| ジョブ名 | 個数 | 追加で要る値（第1段の3つを除く） |
|---|---:|---|
| `token-encryption-healthcheck` | 2 | `TOKEN_ENCRYPTION_KEY` `SLACK_WEBHOOK_URL_ALERT` |
| `gmail-watch-renewal` | 4 | ＋`GOOGLE_OAUTH_CLIENT_ID` `..._SECRET` |
| `zoho-webhook-renewal` | 5 | Zoho一式5つ（DB不要） |
| `incident-digest` | 8 | ＋Notion・Slack Bot・`SYNC_SYSTEM_ID` |
| `project-mirror-reconcile` | 10 | ＋`PROJECT_MIRROR_*` 2つ |
| `relation-sync-reconcile` | 10 | ＋`RELATION_SYNC_*` 2つ |
| `gmail-sync` | 14 | ＋Google OAuth・web-engagement webhook |
| `daily-batch` | 15 | ＋Googleサービスアカウント・売上目標Notion |
| `spreadsheet-outbox-drain` | 32 | kintone・Zoho・Sheets・IDマッピング全部 |

再現するには `python3 scripts/cloud_run/list_cron_env.py`。

#### 最初に移す1本は `token-encryption-healthcheck`

```
   選んだ理由                        避けたかったこと
   ─────────────────────────────    ─────────────────────────────
   環境変数が2つで最少               32個を揃えないと動かない
   DBに触らない                      移行の失敗とDB到達の失敗が混ざる
   書き込みが1つも無い                二重実行で本番データが壊れる
   （やるのは暗号化の往復だけ）        （Vercelと重なる瞬間が必ずある）
```

**★ ただしこの1本は、Vercel側を止めない。**
このcronは「**自分が動いている環境の鍵**」を診断するもので、鍵を実際に使う
Gmail連携・見積書承認はまだVercelにいる。Vercelのcronを消すと、
**Vercelの鍵を誰も見ていない状態**になる。読み取りだけで副作用が無いので、
両方で走らせるのが正しい姿。

```
   いま           Vercel cron ──▶ Vercelの鍵を診る          ✅
   1本目のあと     Vercel cron ──▶ Vercelの鍵を診る          ✅ 残す
                  Scheduler   ──▶ Cloud Runの鍵を診る       ✅ 追加
   gmail-sync移行後 Vercel側を落とす（鍵を使う側が居なくなるので）
```

つまり1本目は「Scheduler → Cloud Run → IAM/OIDC の経路が通ることを、
**壊れても何も失わない的で確かめる**」ための1本。Vercelから実際に剥がす
最初の1本は、`gmail-watch-renewal` 以降で改めて選ぶ。

#### ★ `CRON_SECRET` は要らなくなった（2026-09-08に撤回）

`deploy.sh` が4つ目に要求していたが、外した。

```
   Scheduler ──OIDC(IAM)──▶ Cloud Run ──▶ X-Cloud-Scheduler: true
                                           ＋ CLOUD_RUN_SCHEDULER_AUTH_ENABLED=true
                                           ＝ ここで認証が済んでいる
   Scheduler は CRON_SECRET を送らない  → Cloud Run に置いても使われない
```

しかもVercelの `CRON_SECRET` はSensitive指定で読み戻せない。置いたままだと
**取れない値を待つだけでデプロイが進まない。** 無い状態でもヘッダーの無い
呼び出しは401で閉じる（`src/api/auth.py`）ので、緩めたことにはならない。

#### 登録した値と、まだ確かめていないこと

| シークレット | 出どころ | 状態 |
|---|---|---|
| `TOKEN_ENCRYPTION_KEY` | `dashboard/.env.local`（64桁hex） | 登録済み・**本番と同一かは未検証** |
| `SLACK_WEBHOOK_URL_ALERT` | `config/.env`（hooks.slack.com） | 登録済み |

**★ 鍵が本番と違っても、このヘルスチェックは緑になる。**
やっているのは「自分で暗号化して自分で復号する」往復なので、**どんな正しい鍵でも
通ってしまう**。本番と同じ鍵かどうかは、DBに入っている既存の暗号文が解けるかで
しか分からない。**activate の前に1回だけ確かめること**（読み取りのみ）。

```
   cd ~/crm-sfa-integration && .venv/bin/python - <<'EOF'
   import os, pathlib, sys; sys.path.insert(0, ".")
   for l in pathlib.Path("dashboard/.env.local").read_text().splitlines():
       if l.startswith(("DATABASE_URL=", "TOKEN_ENCRYPTION_KEY=")):
           k, v = l.split("=", 1); os.environ[k] = v.strip().strip('"')
   from src.gmail_sync.token_crypto import decrypt_token
   import psycopg
   with psycopg.connect(os.environ["DATABASE_URL"]) as c, c.cursor() as cur:
       cur.execute('select "refreshTokenEnc" from "RepGmailConnection" limit 3')
       for (enc,) in cur.fetchall():
           try: decrypt_token(enc); print("復号OK")
           except Exception as e: print("復号NG", type(e).__name__)
   EOF
```

**「復号NG」が出たら、その鍵は本番の鍵ではない。** Vercelから読み戻せないので、
dashboard側の発行元（`dashboard/lib/tokenCrypto.ts` を使っている環境）から
取り直して `bootstrap_secrets.sh TOKEN_ENCRYPTION_KEY` で版を足し直す。

```
  ⓪ deploy   ★先に再デプロイ。今のリビジョンには
             CLOUD_RUN_SCHEDULER_AUTH_ENABLED が無く、必ず401になる
  ① plan     何を移すか・時刻・URLを表示
  ② create   停止状態のジョブを作成（定時実行はまだ始まらない）
  ③ run      手動実行し、Cloud LoggingでHTTP 200を確認
  ④ Vercel   vercel.jsonから同じpathだけを削除してデプロイ
             ← token-encryption-healthcheck だけは**やらない**（上記の理由）
  ⑤ activate 定時実行を有効にし、次の実行が200になったことを確認
```

```
  bash scripts/cloud_run/deploy.sh --dry-run
  bash scripts/cloud_run/deploy.sh
  bash scripts/cloud_run/manage_scheduler_job.sh plan   token-encryption-healthcheck
  bash scripts/cloud_run/manage_scheduler_job.sh create token-encryption-healthcheck
  bash scripts/cloud_run/manage_scheduler_job.sh run    token-encryption-healthcheck
  bash scripts/cloud_run/manage_scheduler_job.sh activate token-encryption-healthcheck
```

**★ 宛先URLは組み立てず、必ず `gcloud run services describe` に聞く**
（2026-09-08に修正）。本番のURLは `crm-sfa-backend-gqk5cir6ea-uk.a.run.app` という
旧形式で、プロジェクト番号から組み立てた
`crm-sfa-backend-1052958139029.us-east4.run.app` は**存在しない**。
組み立てたままだと `create` は通り、`run` だけが原因不明で失敗する。

9本の対応表（時刻はすべてUTC、Vercelの設定をそのまま引き継ぐ）:

| ジョブ名 | path | UTC | 状態 |
|---|---|---:|---|
| `daily-batch` | `/api/cron/daily-batch` | 10:00 | 未移行 |
| `zoho-webhook-renewal` | `/api/cron/zoho-webhook-renewal` | 20:00 | 未移行 |
| `token-encryption-healthcheck` | `/api/cron/token-encryption-healthcheck` | 01:00 | **1本目（Vercelは残す）** |
| `gmail-sync` | `/api/cron/gmail-sync` | 03:00 | 未移行 |
| `gmail-watch-renewal` | `/api/cron/gmail-watch-renewal` | 02:00 | 未移行 |
| `incident-digest` | `/api/cron/incident-digest` | 04:00 | 未移行 |
| `project-mirror-reconcile` | `/api/cron/project-mirror-reconcile` | 18:00 | 未移行 |
| `relation-sync-reconcile` | `/api/cron/relation-sync-reconcile` | 19:00 | 未移行 |
| `spreadsheet-outbox-drain` | `/api/cron/spreadsheet-outbox-drain` | 17:00 | 未移行 |

各cron入口はVercelまたはCloud Schedulerから呼ばれる。移行済みかどうかと、障害時に
確認する実行履歴はこの表とCloud Schedulerのジョブ詳細を正本にする。

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
