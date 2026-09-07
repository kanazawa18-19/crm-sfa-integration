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
# ★ URLは組み立てず、必ずgcloudに聞く（2026-09-08）。
#   Cloud RunのURLには2つの形式があり、どちらが割り当てられるかは選べない。
#     新形式  https://<サービス名>-<プロジェクト番号>.<リージョン>.run.app
#     旧形式  https://<サービス名>-<ハッシュ>-<リージョン略号>.a.run.app  ← 本番はこちら
#   実測（2026-09-08）:
#     組み立て https://crm-sfa-backend-1052958139029.us-east4.run.app  ← 存在しない
#     実際     https://crm-sfa-backend-gqk5cir6ea-uk.a.run.app
#   組み立てた側は宛先ホストが実在しないうえ、--oidc-token-audience も食い違うため、
#   createは通るのにrunだけが原因不明で失敗する（既に一度踏んだ404と同じ形）。
SERVICE_URL="$(gcloud run services describe "${CLOUD_RUN_SERVICE}" \
  --project="${GCP_PROJECT_ID}" \
  --region="${GCP_REGION}" \
  --format='value(status.url)' 2>/dev/null || true)"

# URLが要るのは create（宛先とaudienceを固定する）だけ。plan / run / activate は
# ジョブ名だけで動くので、未デプロイでも下見はできるようにしておく。
if [[ -z "${SERVICE_URL}" && "${ACTION}" == "create" ]]; then
  cat >&2 <<ERROR
ERROR: Cloud Runサービス ${CLOUD_RUN_SERVICE} のURLを取得できませんでした。
  ・まだデプロイしていない  → bash scripts/cloud_run/deploy.sh
  ・リージョン違い          → scripts/cloud_run/config.sh の GCP_REGION=${GCP_REGION}
ERROR
  exit 1
fi

TARGET_URL="${SERVICE_URL:-（未デプロイのため不明）}/api/cron/${PATH_SUFFIX}"

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

  # ★ 1本目だけは手順3を飛ばす。詳しくは docs/cloud_run_migration_note.md の第2段。
  if [[ "${JOB_KEY}" == "token-encryption-healthcheck" ]]; then
    cat <<'KEEP'
★ このジョブは手順3をやらない（Vercel側のcronを残す）。
  診断しているのは「自分が動いている環境の TOKEN_ENCRYPTION_KEY」で、
  鍵を実際に使うGmail連携・見積書承認はまだVercelにいる。Vercelのcronを消すと
  Vercelの鍵を誰も見ていない状態になる。読み取りだけで副作用が無いので、
  両方で走らせるのが正しい。Vercel側を落とすのはgmail-syncを移すとき。

★ activate の前に、登録した鍵が本番と同じかを1回だけ確かめること。
  自分で暗号化して自分で復号する往復なので、鍵が違っても緑になる。
  手順は docs/cloud_run_migration_note.md の「登録した値と、まだ確かめていないこと」。
KEEP
  fi
  exit 0
fi

# ────────────────────────────────────────────────────────────────
# 定刻ガード（2026-09-08、Gemini Proの指摘を反映）
# ────────────────────────────────────────────────────────────────
# 移行作業は「Vercelを止める」と「Schedulerを動かす」の2手に分かれる。その隙間に
# 本来の定刻が来ると、片方だけが走る（取りこぼし）か、両方が走る（二重起動）。
#
#   定刻 10:00 の例
#     09:58 Vercel削除 → 10:00 まだ activate していない  → 取りこぼし
#     09:58 activate   → 10:00 VercelとSchedulerの両方   → 二重起動
#
# 人間が手で進める以上、時間帯そのものを避けるのが確実なので、
# 定刻の前後1時間はコマンド自体を止める。plan（下見）は読み取りだけなので通す。
if [[ "${SKIP_SCHEDULE_GUARD:-}" == "1" ]]; then
  echo "⚠ SKIP_SCHEDULE_GUARD=1 のため定刻ガードを飛ばします。二重起動に注意してください。" >&2
else
  GUARD_MSG="$(python3 - "${SCHEDULE}" <<'GUARD'
import datetime
import sys

# SCHEDULE は "分 時 * * *"（UTC）の形しか使っていない。
minute, hour = sys.argv[1].split()[:2]
now = datetime.datetime.now(datetime.timezone.utc)
target = now.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0)

# 日をまたぐ場合も見るので、前日・当日・翌日の3つで一番近いものを取る。
diff_min = min(
    abs((target + datetime.timedelta(days=d) - now).total_seconds()) / 60
    for d in (-1, 0, 1)
)
if diff_min < 60:
    print(
        f"定刻 {hour.zfill(2)}:{minute.zfill(2)} UTC まで残り {diff_min:.0f} 分です"
        f"（現在 {now:%H:%M} UTC）"
    )
GUARD
)"
  if [[ -n "${GUARD_MSG}" ]]; then
    cat >&2 <<ERROR

────────────────────────────────────────────────────────────────
 定刻に近すぎるので止めました。
────────────────────────────────────────────────────────────────
 ${GUARD_MSG}

 この時間帯に移行を進めると、VercelとCloud Schedulerの
 どちらも走らない（取りこぼし）か、両方走る（二重起動）ことがあります。
 定刻の前後1時間を外してからやり直してください。

 どうしても今やる必要がある場合だけ:
   SKIP_SCHEDULE_GUARD=1 bash scripts/cloud_run/manage_scheduler_job.sh ${ACTION} ${JOB_KEY}
────────────────────────────────────────────────────────────────
ERROR
    exit 1
  fi
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
