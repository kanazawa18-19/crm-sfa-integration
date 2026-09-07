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
#   4. scripts/cloud_run/bootstrap_secrets.sh で3つのシークレットを登録済み
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

# 第1段で必要な認証情報だけ。増やすときはここに足す。
# 形式: <コンテナ内の環境変数名>=<Secret Manager のシークレット名>:<版>
SECRETS="DATABASE_URL=DATABASE_URL:latest"
SECRETS="${SECRETS},DATABASE_URL_UNPOOLED=DATABASE_URL_UNPOOLED:latest"
SECRETS="${SECRETS},DASHBOARD_API_TOKEN=DASHBOARD_API_TOKEN:latest"

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
 タイムアウト : 3600秒（Vercelの300秒制限を外すのが移行の目的の1つ）
 dry-run      : $([[ "${DRY_RUN}" -eq 1 ]] && echo "はい（何もしない）" || echo "いいえ（実行する）")
────────────────────────────────────────────────────────────────
INFO

# ここから先は課金が発生する。dry-run でも --yes でもないときだけ、手で止まって確認する。
if [[ "${DRY_RUN}" -eq 0 && "${ASSUME_YES}" -eq 0 ]]; then
  cat <<'CONFIRM'

これから次の4つを実行します。いずれも課金対象です。
  1. GCPのAPIを有効化（Cloud Run / Cloud Build / Artifact Registry / Secret Manager）
  2. 専用のサービスアカウントを作成
  3. シークレット3つに読み取り権限を付与
  4. ソースをアップロードしてイメージをビルドし、Cloud Run へデプロイ

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
  --project="${GCP_PROJECT_ID}"

# --- 2. 専用サービスアカウント（無ければ作る） ----------------------------------
if ! gcloud iam service-accounts describe "${SA_EMAIL}" --project="${GCP_PROJECT_ID}" >/dev/null 2>&1; then
  run gcloud iam service-accounts create "${SERVICE_ACCOUNT}" \
    --project="${GCP_PROJECT_ID}" \
    --display-name="crm-sfa-integration backend (Cloud Run)"
else
  echo "= サービスアカウントは既にあります: ${SA_EMAIL}"
fi

# --- 3. シークレットの読み取り権限（シークレット単位で付ける。プロジェクト全体には付けない） ---
for pair in ${SECRETS//,/ }; do
  secret_name="${pair#*=}"      # DATABASE_URL:latest
  secret_name="${secret_name%%:*}"  # DATABASE_URL
  run gcloud secrets add-iam-policy-binding "${secret_name}" \
    --project="${GCP_PROJECT_ID}" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="roles/secretmanager.secretAccessor" \
    --condition=None
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
  --concurrency=20

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
