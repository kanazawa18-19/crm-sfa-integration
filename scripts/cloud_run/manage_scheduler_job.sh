#!/usr/bin/env bash
# Cloud Schedulerへのcron移行を、1本ずつ安全に進めるための補助スクリプト。
#
# 使い方:
#   bash scripts/cloud_run/manage_scheduler_job.sh plan daily-batch
#   bash scripts/cloud_run/manage_scheduler_job.sh create daily-batch
#   bash scripts/cloud_run/manage_scheduler_job.sh run daily-batch
#
# Cloud SchedulerはCloud Run IAMのOIDC認証で到達を制限する。合言葉はジョブ設定へ保存しない。
# 試運転の影響承認と業務結果照合は docs/cloud_scheduler_trial.md を参照。HTTP 200だけで移行しない。

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/cloud_run/config.sh
source "${SCRIPT_DIR}/config.sh"

ACTION="${1:-}"
JOB_KEY="${2:-}"
ALLOW_BUSINESS_WRITES="${3:-}"
if [[ $# -gt 3 || ( -n "${ALLOW_BUSINESS_WRITES}" && ( "${ACTION}" != "run" || "${ALLOW_BUSINESS_WRITES}" != "--allow-business-writes" ) ) ]]; then
  echo "ERROR: 不明な引数です。" >&2
  exit 1
fi

usage() {
  cat <<'USAGE'
使い方:
  bash scripts/cloud_run/manage_scheduler_job.sh plan <ジョブ名>
  bash scripts/cloud_run/manage_scheduler_job.sh create <ジョブ名>
  bash scripts/cloud_run/manage_scheduler_job.sh run <ジョブ名> [--allow-business-writes]
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

# 実行・有効化の前にも宛先を照合する。planだけはURL不明でも下見できる。
if [[ -z "${SERVICE_URL}" && "${ACTION}" != "plan" ]]; then
  cat >&2 <<ERROR
ERROR: Cloud Runサービス ${CLOUD_RUN_SERVICE} のURLを取得できませんでした。
  ・認証失効・権限不足      → gcloudのログイン状態とサービス閲覧権限を確認
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
  2. docs/cloud_scheduler_trial.md で影響を確認し、業務ジョブは実行許可を得る
     run で試運転後、HTTP状態に加えて業務結果を照合する（HTTP 200だけでは成功扱いしない）
  3. 本番移行は別イシュー。結果確認と移行許可の後、Vercel側の同じpathを停止する
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

★ 鍵の照合は解決済み（2026-09-08）。再探索・再復号確認は不要。
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

# 仮日程の前後24時間は、有効化の短い隙間にも定期実行が起き得るので拒否する。
# この判定は定刻ガードの解除設定でも省略しない。
python3 - <<'SAFE_DATE'
import datetime
import sys
now = datetime.datetime.now(datetime.timezone.utc)
for year in range(now.year - 8, now.year + 9):
    try:
        scheduled = datetime.datetime(year, 2, 29, tzinfo=datetime.timezone.utc)
    except ValueError:
        continue
    if abs((scheduled - now).total_seconds()) <= 86400:
        sys.exit("ERROR: 仮日程（2月29日 UTC）の前後24時間は操作できません。")
SAFE_DATE

scheduler() {
  gcloud scheduler jobs "$@" "${SCHEDULER_JOB}" \
    --project="${GCP_PROJECT_ID}" --location="${GCP_REGION}"
}

# 現在の設定が移行用の停止ジョブと完全に一致する場合だけ操作する。
if [[ "${ACTION}" == "run" || "${ACTION}" == "activate" ]]; then
  JOB_JSON="$(scheduler describe --format=json)"
  printf '%s' "${JOB_JSON}" | python3 -c '
import json, sys
job = json.load(sys.stdin)
target, audience, account = sys.argv[1:]
http = job.get("httpTarget", {})
expected = {
    "state": (job.get("state"), "PAUSED"),
    "schedule": (job.get("schedule"), "0 0 29 2 *"),
    "timeZone": (job.get("timeZone"), "Etc/UTC"),
    "uri": (http.get("uri"), target),
    "httpMethod": (http.get("httpMethod"), "GET"),
    "X-Cloud-Scheduler": ({key.lower(): value for key, value in http.get("headers", {}).items()}.get("x-cloud-scheduler"), "true"),
    "oidc.audience": (http.get("oidcToken", {}).get("audience"), audience),
    "oidc.serviceAccountEmail": (http.get("oidcToken", {}).get("serviceAccountEmail"), account),
    "retryCount": (job.get("retryConfig", {}).get("retryCount", 0), 0),
    "maxRetryDuration": (job.get("retryConfig", {}).get("maxRetryDuration", "0s"), "0s"),
}
errors = [key for key, (actual, required) in expected.items() if actual != required]
if http.get("body") or http.get("oauthToken"):
    errors.append("body/oauthToken")
if errors:
    sys.exit("ERROR: 移行用停止ジョブの設定と不一致: " + ", ".join(errors))
' "${TARGET_URL}" "${SERVICE_URL}" "${SCHEDULER_SA_EMAIL}"
fi

if [[ "${ACTION}" == "run" ]]; then
  if [[ "${JOB_KEY}" != "token-encryption-healthcheck" && "${ALLOW_BUSINESS_WRITES}" != "--allow-business-writes" ]]; then
    echo "ERROR: 本番書き込みを伴います。影響確認と実行許可の後に --allow-business-writes を指定してください（フラグは許可の代わりではありません）。" >&2
    exit 1
  fi
  cleanup() {
    local result=$? stopped
    trap - EXIT
    trap '' INT TERM
    if ! scheduler pause; then
      echo "ERROR: 停止要求が失敗しました。" >&2
      result=1
    fi
    stopped="$(scheduler describe --format='value(state)')" || stopped="不明"
    if [[ "${stopped}" != "PAUSED" || ${result} -ne 0 ]]; then
      echo "ERROR: 手動実行または停止処理に失敗しました。現在状態: ${stopped}" >&2
      echo "手動復旧: gcloud scheduler jobs pause '${SCHEDULER_JOB}' --project='${GCP_PROJECT_ID}' --location='${GCP_REGION}'" >&2
      result=1
    fi
    exit "${result}"
  }
  trap cleanup EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  STARTED_AT="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  echo "実行後のpauseは処理をキャンセルしません。失敗・中断後の再runも二重実行になり得るため、実行ログと業務結果を先に確認してください。" >&2
  scheduler resume
  scheduler run
  echo "手動実行の要求が受け付けられました。処理完了を意味しません。終了時にジョブを停止しますが、進行中の処理はキャンセルされません。"
  echo "失敗・中断後も実行済みの可能性があります。再runは二重実行になるため、先に実行ログと業務結果を確認してください。"
  echo "次のコマンドは ${STARTED_AT} 以降のHTTP状態だけを表示します。応答本文は取得しません。"
  echo "業務結果の照合は docs/cloud_scheduler_trial.md に従ってください。本文を取るための再runは行わないでください。"
  echo "gcloud logging read 'resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"${CLOUD_RUN_SERVICE}\" AND httpRequest.requestUrl:\"/api/cron/${PATH_SUFFIX}\" AND timestamp>=\"${STARTED_AT}\"' --project=\"${GCP_PROJECT_ID}\" --limit=10 --format='table(timestamp,httpRequest.status)'"
  exit 0
fi

if [[ "${ACTION}" == "activate" ]]; then
  gcloud scheduler jobs update http "${SCHEDULER_JOB}" \
    --project="${GCP_PROJECT_ID}" --location="${GCP_REGION}" \
    --schedule="${SCHEDULE}" --time-zone="Etc/UTC"
  if ! scheduler resume; then
    echo "ERROR: 本日程への更新後、有効化に失敗しました。日程は自動で戻しません。" >&2
    echo "現在の状態と日程:" >&2
    scheduler describe --format='yaml(state,schedule,timeZone,httpTarget.uri,httpTarget.httpMethod,httpTarget.oidcToken.audience,httpTarget.oidcToken.serviceAccountEmail)' >&2 || \
      echo "ERROR: 現在の設定を取得できませんでした。認証・権限を確認してください。" >&2
    echo "復旧前に本日程・宛先・OIDC設定と旧cronの停止を確認してください（診断cronはVercel側を残します）。" >&2
    echo "確認後の手動復旧: gcloud scheduler jobs resume '${SCHEDULER_JOB}' --project='${GCP_PROJECT_ID}' --location='${GCP_REGION}'" >&2
    exit 1
  fi
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
  --max-retry-attempts=0 \
  --max-retry-duration=0s \
  --oidc-service-account-email="${SCHEDULER_SA_EMAIL}" \
  --oidc-token-audience="${SERVICE_URL}" \
  --headers="X-Cloud-Scheduler=true"

gcloud scheduler jobs pause "${SCHEDULER_JOB}" \
  --project="${GCP_PROJECT_ID}" --location="${GCP_REGION}"

echo "停止状態で作成しました。業務ジョブの試運転は影響確認・実行許可と --allow-business-writes が必要です。"
echo "次に 'bash scripts/cloud_run/manage_scheduler_job.sh run ${JOB_KEY}' を実行します。HTTP状態と業務結果の照合は docs/cloud_scheduler_trial.md を参照してください。"
