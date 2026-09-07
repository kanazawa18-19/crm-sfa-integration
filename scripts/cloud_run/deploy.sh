#!/usr/bin/env bash
# crm-sfa-integration のバックエンドを Cloud Run へデプロイする（2026-09-07、移行の第1段）。
#
# ★ この第1段では Vercel を一切触らない。Cloud Run 側を並走させて、
#   「読み取り専用の診断エンドポイント1本」が同じ結果を返すことだけを確かめる。
#   Webhookの受け口（kintone/Zoho/Notionに登録済みのURL）はVercelのまま。
#
# 前提:
#   1. gcloud が入っていて `gcloud auth login` 済み
#   2. `gcloud config set project <PROJECT_ID>` 済み（または GCP_PROJECT_ID を指定）
#   3. 課金が有効なプロジェクト（Cloud Run / Cloud Build は課金必須）
#   4. scripts/cloud_run/bootstrap_secrets.sh で必要なシークレットを登録済み
#
# 使い方:
#   bash scripts/cloud_run/deploy.sh --dry-run  # コマンドを出すだけ。何もしない（まずこれ）
#   bash scripts/cloud_run/deploy.sh            # 実行内容を出し、yes と打つまで止まる
#   bash scripts/cloud_run/deploy.sh --yes      # 確認を飛ばす（自動実行用。手では使わない）
#
# ★ 課金が発生する操作を含む（Cloud Build のビルド・Artifact Registry の保管・Cloud Run）。
#   --min-instances=0 なので待機中はほぼ無料だが、ビルドと保管には少額かかる。
#   やめるときの片付けは docs/cloud_run_migration_note.md の「試してやめるとき」を見ること。

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
# shellcheck source=scripts/cloud_run/config.sh
source "${SCRIPT_DIR}/config.sh"

DRY_RUN=0
ASSUME_YES=0
case "${1:-}" in
  --dry-run) DRY_RUN=1 ;;
  --yes|-y)  ASSUME_YES=1 ;;
  "")        ;;
  *)
    echo "ERROR: 知らない引数です: ${1}" >&2
    echo "  使えるのは --dry-run / --yes だけです。" >&2
    exit 1
    ;;
esac

# 専用のサービスアカウント。既定のCompute SAは権限が広すぎるので使わない。
SERVICE_ACCOUNT="${CLOUD_RUN_SERVICE}-sa"
SA_EMAIL="${SERVICE_ACCOUNT}@${GCP_PROJECT_ID}.iam.gserviceaccount.com"

# 第1段のDB診断3つと、第2段のCloud Scheduler認証に必要な値。
# 形式: <コンテナ内の環境変数名>=<Secret Manager のシークレット名>:<版>
SECRETS="DATABASE_URL=DATABASE_URL:latest"
SECRETS="${SECRETS},DATABASE_URL_UNPOOLED=DATABASE_URL_UNPOOLED:latest"
SECRETS="${SECRETS},DASHBOARD_API_TOKEN=DASHBOARD_API_TOKEN:latest"
SECRETS="${SECRETS},CRON_SECRET=CRON_SECRET:latest"

run() {
  echo "+ $*"
  if [[ "${DRY_RUN}" -eq 0 ]]; then
    "$@"
  fi
}

require_gcloud

cat <<INFO
────────────────────────────────────────────────────────────────
 プロジェクト : ${GCP_PROJECT_ID}
 リージョン   : ${GCP_REGION}
 サービス名   : ${CLOUD_RUN_SERVICE}
 サービスAC   : ${SA_EMAIL}
 認証         : IAM必須（--no-allow-unauthenticated）。公開しない
 疎通確認     : 実行用SAのIDトークンを使う（本人には発行権限だけ付与）
 タイムアウト : 3600秒（Vercelの300秒制限を外すのが移行の目的の1つ）
 dry-run      : $([[ "${DRY_RUN}" -eq 1 ]] && echo "はい（何もしない）" || echo "いいえ（実行する）")
────────────────────────────────────────────────────────────────
INFO

# ここから先は課金が発生する。dry-run でも --yes でもないときだけ、手で止まって確認する。
if [[ "${DRY_RUN}" -eq 0 && "${ASSUME_YES}" -eq 0 ]]; then
  cat <<'CONFIRM'

これから次の6つを実行します。API・ビルド・Cloud Runは課金対象です。
  1. GCPのAPIを有効化（Cloud Run / Cloud Build / Artifact Registry / Secret Manager）
  2. 専用のサービスアカウントを作成
  3. シークレット4つに読み取り権限を付与
  4. ソースをアップロードしてイメージをビルドし、Cloud Run へデプロイ
  5. 実行用サービスアカウント自身に、このサービスだけの呼び出し権限を付与
  6. 実行者に、実行用サービスアカウントのIDトークン発行権限だけを付与

まだ何も実行していません。中身だけ見たいなら Ctrl-C で抜けて
`bash scripts/cloud_run/deploy.sh --dry-run` を先に実行してください。

CONFIRM
  read -r -p "続行しますか？ yes と入力してください: " _answer
  if [[ "${_answer}" != "yes" ]]; then
    echo "中止しました。何も実行していません。"
    exit 0
  fi
  echo
fi

# --- 1. 必要なAPIを有効化（何度実行しても安全） ---------------------------------
run gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com \
  iamcredentials.googleapis.com \
  --project="${GCP_PROJECT_ID}"

# --- 2. 専用サービスアカウント（無ければ作る） ----------------------------------
if ! gcloud iam service-accounts describe "${SA_EMAIL}" --project="${GCP_PROJECT_ID}" >/dev/null 2>&1; then
  run gcloud iam service-accounts create "${SERVICE_ACCOUNT}" \
    --project="${GCP_PROJECT_ID}" \
    --display-name="crm-sfa-integration backend (Cloud Run)"

  # ★ 作った直後は、まだ「存在しない」と返ってくることがある（GCP側の反映待ち）。
  #   待たずに権限を付けにいくと 400 "Service account ... does not exist" で落ちる。
  #   2026-09-07 に実際に踏んだので、見えるようになるまで待つ。
  if [[ "${DRY_RUN}" -eq 0 ]]; then
    echo -n "  サービスアカウントの反映を待っています"
    for _i in $(seq 1 30); do
      if gcloud iam service-accounts describe "${SA_EMAIL}" --project="${GCP_PROJECT_ID}" >/dev/null 2>&1; then
        echo " → できました"
        break
      fi
      echo -n "."
      sleep 2
    done
  fi
else
  echo "= サービスアカウントは既にあります: ${SA_EMAIL}"
fi

# --- 3. シークレットの読み取り権限（シークレット単位で付ける。プロジェクト全体には付けない） ---
for pair in ${SECRETS//,/ }; do
  secret_name="${pair#*=}"      # DATABASE_URL:latest
  secret_name="${secret_name%%:*}"  # DATABASE_URL
  # 反映待ちで一度は失敗しうるので、数回やり直す（冪等な操作なので繰り返して安全）。
  for _try in 1 2 3 4 5; do
    if [[ "${DRY_RUN}" -eq 1 ]]; then
      run gcloud secrets add-iam-policy-binding "${secret_name}" \
        --project="${GCP_PROJECT_ID}" \
        --member="serviceAccount:${SA_EMAIL}" \
        --role="roles/secretmanager.secretAccessor" \
        --condition=None
      break
    fi
    if gcloud secrets add-iam-policy-binding "${secret_name}" \
        --project="${GCP_PROJECT_ID}" \
        --member="serviceAccount:${SA_EMAIL}" \
        --role="roles/secretmanager.secretAccessor" \
        --condition=None >/dev/null 2>&1; then
      echo "+ ${secret_name} に読み取り権限を付けました"
      break
    fi
    if [[ "${_try}" -eq 5 ]]; then
      echo "ERROR: ${secret_name} への権限付与が5回とも失敗しました。" >&2
      echo "  → シークレットが存在するか確認してください:" >&2
      echo "     bash scripts/cloud_run/bootstrap_secrets.sh --list" >&2
      exit 1
    fi
    echo "  ${secret_name} の権限付与を再試行します（${_try}回目・反映待ち）"
    sleep 5
  done
done

# --- 4. デプロイ（Dockerfileから Cloud Build がイメージを作る） ------------------
# ここで転ぶとしたら、ほぼ Dockerfile の初ビルドが原因。
# 失敗したときに「どこを見ればいいか」が分からないと詰まるので、案内を出してから抜ける。
on_deploy_failure() {
  cat <<'FAIL' >&2

────────────────────────────────────────────────────────────────
 デプロイに失敗しました。まず見るのは Cloud Build のログです。
────────────────────────────────────────────────────────────────
  直近のビルドを一覧する:
    gcloud builds list --limit=3
  そのビルドのログを読む（ID は上の一覧の1列目）:
    gcloud builds log <BUILD_ID>

 よくある原因:
  ・課金が有効になっていない        → GCPコンソールの「お支払い」を確認
  ・権限が足りない                  → 本人のアカウントに Owner か
                                      「Cloud Run 管理者＋Cloud Build 編集者」が要る
  ・Dockerfile のビルドで転んだ      → 上のログに pip のエラーが出ている
────────────────────────────────────────────────────────────────
FAIL
  exit 1
}
trap 'on_deploy_failure' ERR

run gcloud run deploy "${CLOUD_RUN_SERVICE}" \
  --project="${GCP_PROJECT_ID}" \
  --region="${GCP_REGION}" \
  --source="${REPO_ROOT}" \
  --service-account="${SA_EMAIL}" \
  --no-allow-unauthenticated \
  --set-secrets="${SECRETS}" \
  --timeout=3600 \
  --memory=1Gi \
  --cpu=1 \
  --min-instances=0 \
  --max-instances=2 \
  --concurrency=20 \
  --quiet

trap - ERR

# 非公開サービスを smoke_test.sh から呼ぶための最小権限。
# 一般ユーザーの `gcloud auth print-identity-token` は audience が Cloud Run URLではなく、
# この環境では404になる。実行用SA自身を invoker にし、デプロイした本人には
# IAM Credentials APIでそのSAのIDトークンを発行する権限だけを付ける。
DEPLOY_ACCOUNT="$(gcloud config get-value account 2>/dev/null)"
if [[ "${DEPLOY_ACCOUNT}" == *.gserviceaccount.com ]]; then
  TOKEN_CREATOR_MEMBER="serviceAccount:${DEPLOY_ACCOUNT}"
else
  TOKEN_CREATOR_MEMBER="user:${DEPLOY_ACCOUNT}"
fi

on_iam_failure() {
  cat <<'FAIL' >&2

────────────────────────────────────────────────────────────────
 Cloud Run本体のデプロイは完了しましたが、疎通確認用の権限設定に失敗しました。
 deploy.sh は冪等なので、そのまま再実行して構いません。既存サービスを更新した後、
 同じ権限設定をもう一度試します。
────────────────────────────────────────────────────────────────
FAIL
  exit 1
}
if [[ "${DRY_RUN}" -eq 0 ]]; then
  trap 'on_iam_failure' ERR
fi
run gcloud run services add-iam-policy-binding "${CLOUD_RUN_SERVICE}" \
  --project="${GCP_PROJECT_ID}" \
  --region="${GCP_REGION}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/run.invoker" \
  --quiet
run gcloud iam service-accounts add-iam-policy-binding "${SA_EMAIL}" \
  --project="${GCP_PROJECT_ID}" \
  --member="${TOKEN_CREATOR_MEMBER}" \
  --role="roles/iam.serviceAccountOpenIdTokenCreator" \
  --condition=None \
  --quiet
trap - ERR

if [[ "${DRY_RUN}" -eq 0 ]]; then
  echo
  echo "URL:"
  gcloud run services describe "${CLOUD_RUN_SERVICE}" \
    --project="${GCP_PROJECT_ID}" --region="${GCP_REGION}" \
    --format="value(status.url)"
  echo
  echo "次: bash scripts/cloud_run/smoke_test.sh で疎通を確認してください。"
fi
