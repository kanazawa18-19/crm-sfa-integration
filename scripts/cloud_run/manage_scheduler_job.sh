#!/usr/bin/env bash
# Cloud Schedulerへのcron移行を、1本ずつ安全に進めるための補助スクリプト。
#
# 使い方:
#   bash scripts/cloud_run/manage_scheduler_job.sh plan daily-batch
#   bash scripts/cloud_run/manage_scheduler_job.sh create daily-batch
#   bash scripts/cloud_run/manage_scheduler_job.sh run daily-batch
#
# Cloud SchedulerはCloud Run IAMのOIDC認証で到達を制限する。合言葉はジョブ設定へ保存しない。
# create → runがCloud Loggingで200を返すことを確認するまでは、vercel.jsonを触らない。

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/cloud_run/config.sh
source "${SCRIPT_DIR}/config.sh"

ACTION="${1:-}"
JOB_KEY="${2:-}"

usage() {
  cat <<'USAGE'
使い方:
  bash scripts/cloud_run/manage_scheduler_job.sh plan <ジョブ名>
  bash scripts/cloud_run/manage_scheduler_job.sh create <ジョブ名>
  bash scripts/cloud_run/manage_scheduler_job.sh run <ジョブ名>
  bash scripts/cloud_run/manage_scheduler_job.sh activate <ジョブ名>

ジョブ名:
  daily-batch / zoho-webhook-renewal / token-encryption-healthcheck
  gmail-sync / gmail-watch-renewal / incident-digest
  project-mirror-reconcile / relation-sync-reconcile / spreadsheet-outbox-drain
USAGE
}

case "${ACTION}" in
  plan|create|run|activate) ;;
  *) usage >&2; exit 1 ;;
esac

case "${JOB_KEY}" in
  daily-batch)                 PATH_SUFFIX="daily-batch";                 SCHEDULE="0 10 * * *" ;;
  zoho-webhook-renewal)        PATH_SUFFIX="zoho-webhook-renewal";        SCHEDULE="0 20 * * *" ;;
  token-encryption-healthcheck) PATH_SUFFIX="token-encryption-healthcheck"; SCHEDULE="0 1 * * *" ;;
  gmail-sync)                  PATH_SUFFIX="gmail-sync";                  SCHEDULE="0 3 * * *" ;;
  gmail-watch-renewal)         PATH_SUFFIX="gmail-watch-renewal";         SCHEDULE="0 2 * * *" ;;
  incident-digest)             PATH_SUFFIX="incident-digest";             SCHEDULE="0 4 * * *" ;;
  project-mirror-reconcile)    PATH_SUFFIX="project-mirror-reconcile";    SCHEDULE="0 18 * * *" ;;
  relation-sync-reconcile)     PATH_SUFFIX="relation-sync-reconcile";     SCHEDULE="0 19 * * *" ;;
  spreadsheet-outbox-drain)    PATH_SUFFIX="spreadsheet-outbox-drain";    SCHEDULE="0 17 * * *" ;;
  *) usage >&2; exit 1 ;;
esac

require_gcloud

SCHEDULER_SA_NAME="${CLOUD_RUN_SERVICE}-scheduler"
SCHEDULER_SA_EMAIL="${SCHEDULER_SA_NAME}@${GCP_PROJECT_ID}.iam.gserviceaccount.com"
SCHEDULER_JOB="${CLOUD_RUN_SERVICE}-${JOB_KEY}"
SERVICE_URL="https://${CLOUD_RUN_SERVICE}-$(gcloud projects describe "${GCP_PROJECT_ID}" --format='value(projectNumber)').${GCP_REGION}.run.app"
TARGET_URL="${SERVICE_URL}/api/cron/${PATH_SUFFIX}"

cat <<INFO
────────────────────────────────────────────────────────────────
 ジョブ名       : ${SCHEDULER_JOB}
 実行先         : ${TARGET_URL}
 実行時刻       : ${SCHEDULE} UTC（Vercelと同じ時刻）
 呼出用AC       : ${SCHEDULER_SA_EMAIL}
INFO

if [[ "${ACTION}" == "plan" ]]; then
  cat <<'PLAN'

移行の順番（この順以外で進めない）:
  1. create で停止状態のCloud Schedulerジョブを作る
  2. run で手動実行し、Cloud LoggingでHTTP 200を確認する
  3. vercel.jsonから同じpathだけを削除してVercelへデプロイする
  4. activate で定時実行を有効にし、次の実行履歴とCloud Loggingを確認する

1〜2で失敗した場合はVercel側を残したまま、Cloud Schedulerジョブを削除または修正する。
PLAN
  exit 0
fi

if [[ "${ACTION}" == "run" ]]; then
  STARTED_AT="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  gcloud scheduler jobs run "${SCHEDULER_JOB}" \
    --project="${GCP_PROJECT_ID}" \
    --location="${GCP_REGION}"
  echo "手動実行を要求しました。次で ${STARTED_AT} 以降のHTTP 200を確認してください:"
  echo "gcloud logging read 'resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"${CLOUD_RUN_SERVICE}\" AND httpRequest.requestUrl:\"/api/cron/${PATH_SUFFIX}\" AND timestamp>=\"${STARTED_AT}\"' --project=\"${GCP_PROJECT_ID}\" --limit=10 --format='table(timestamp,httpRequest.status)'"
  exit 0
fi

if [[ "${ACTION}" == "activate" ]]; then
  gcloud scheduler jobs update http "${SCHEDULER_JOB}" \
    --project="${GCP_PROJECT_ID}" --location="${GCP_REGION}" \
    --schedule="${SCHEDULE}" --time-zone="Etc/UTC"
  gcloud scheduler jobs resume "${SCHEDULER_JOB}" \
    --project="${GCP_PROJECT_ID}" --location="${GCP_REGION}"
  echo "定時実行を有効化しました。Vercel側の同じpathを停止済みであることを確認してください。"
  exit 0
fi

if gcloud scheduler jobs describe "${SCHEDULER_JOB}" \
    --project="${GCP_PROJECT_ID}" --location="${GCP_REGION}" >/dev/null 2>&1; then
  echo "ERROR: ${SCHEDULER_JOB} は既にあります。設定を上書きせず停止しました。" >&2
  exit 1
fi

gcloud services enable cloudscheduler.googleapis.com --project="${GCP_PROJECT_ID}"

if ! gcloud iam service-accounts describe "${SCHEDULER_SA_EMAIL}" --project="${GCP_PROJECT_ID}" >/dev/null 2>&1; then
  gcloud iam service-accounts create "${SCHEDULER_SA_NAME}" \
    --project="${GCP_PROJECT_ID}" \
    --display-name="crm-sfa-integration Cloud Scheduler"
fi

gcloud run services add-iam-policy-binding "${CLOUD_RUN_SERVICE}" \
  --project="${GCP_PROJECT_ID}" \
  --region="${GCP_REGION}" \
  --member="serviceAccount:${SCHEDULER_SA_EMAIL}" \
  --role="roles/run.invoker"

gcloud scheduler jobs create http "${SCHEDULER_JOB}" \
  --project="${GCP_PROJECT_ID}" \
  --location="${GCP_REGION}" \
  --schedule="0 0 29 2 *" \
  --time-zone="Etc/UTC" \
  --uri="${TARGET_URL}" \
  --http-method=GET \
  --attempt-deadline="30m" \
  --oidc-service-account-email="${SCHEDULER_SA_EMAIL}" \
  --oidc-token-audience="${SERVICE_URL}" \
  --headers="X-Cloud-Scheduler=true"

gcloud scheduler jobs pause "${SCHEDULER_JOB}" \
  --project="${GCP_PROJECT_ID}" --location="${GCP_REGION}"

echo "停止状態で作成しました。次に 'bash scripts/cloud_run/manage_scheduler_job.sh run ${JOB_KEY}' を実行し、HTTP 200を確認してください。"
