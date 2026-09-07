#!/usr/bin/env bash
# Cloud Run に載せたバックエンドの到達確認（2026-09-07、移行の第1段）。
#
# 確かめるのは2つだけ。どちらも読み取りで、本番データを書き換えない。
#   1. GET /openapi.json                             … コンテナが起きて応答するか
#   2. GET /api/diagnostics/integrations?only=postgres,advisory_lock
#                                                    … GCPからNeonへ届くか
#      （pooled接続と、排他制御用の非pooled接続の両方を1回で見る）
#
# ★ Authorization ヘッダーが2つ要る理由
#   Cloud RunのIAM認証も、このアプリ自身のトークン認証も、どちらも
#   `Authorization: Bearer ...` を使うのでぶつかる。
#   Googleの仕様で「両方あるときは X-Serverless-Authorization だけを検証し、
#   Authorization はそのままコンテナへ渡す」と決まっているので、
#   GoogleのIDトークンを X-Serverless-Authorization に、
#   アプリのトークンを Authorization に載せる。
#   出典: https://docs.cloud.google.com/run/docs/authenticating/service-to-service
#
# ★ トークンは curl の引数に載せない。
#   `-H "Authorization: Bearer xxx"` と書くと、実行中のあいだ `ps` から値が見える。
#   プロセス置換（--config <(...)）で渡せば、コマンドラインには /dev/fd/63 しか出ない。

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/cloud_run/config.sh
source "${SCRIPT_DIR}/config.sh"
require_gcloud

fail() {
  echo >&2
  echo "────────────────────────────────────────────────────────────────" >&2
  echo " $1" >&2
  shift
  for line in "$@"; do echo " $line" >&2; done
  echo "────────────────────────────────────────────────────────────────" >&2
  exit 1
}

SERVICE_URL="$(gcloud run services describe "${CLOUD_RUN_SERVICE}" \
  --project="${GCP_PROJECT_ID}" --region="${GCP_REGION}" \
  --format='value(status.url)' 2>/dev/null || true)"

if [[ -z "${SERVICE_URL}" ]]; then
  fail "サービス ${CLOUD_RUN_SERVICE} が ${GCP_REGION} に見つかりません。" \
       "" \
       "考えられること:" \
       " ・まだデプロイしていない → bash scripts/cloud_run/deploy.sh" \
       " ・別のリージョンに作った → gcloud run services list" \
       " ・プロジェクトが違う     → gcloud config get-value project"
fi

SERVICE_ACCOUNT="${CLOUD_RUN_SERVICE}-sa@${GCP_PROJECT_ID}.iam.gserviceaccount.com"
ACCESS_TOKEN="$(gcloud auth print-access-token 2>/dev/null || true)"
[[ -n "${ACCESS_TOKEN}" ]] || fail "本人のGoogleアクセストークンを取得できませんでした。" \
  "" "→ gcloud auth login をやり直してください。"

# `gcloud auth print-identity-token --impersonate-service-account` は内部で
# getAccessToken権限まで要求する。ここでは強いToken Creatorロールを避け、
# getOpenIdTokenだけで済むIAM Credentials APIを直接呼ぶ。
ID_TOKEN_RESPONSE="$(curl --silent --show-error --fail-with-body --max-time 30 \
  --config <(printf 'header = "Authorization: Bearer %s"\n' "${ACCESS_TOKEN}") \
  -H 'Content-Type: application/json' \
  --data "{\"audience\":\"${SERVICE_URL}\",\"includeEmail\":true}" \
  "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/${SERVICE_ACCOUNT}:generateIdToken" \
  2>/dev/null || true)"
ID_TOKEN="$(printf '%s' "${ID_TOKEN_RESPONSE}" | python3 -c \
  'import json, sys; print(json.load(sys.stdin).get("token", ""))' 2>/dev/null || true)"
[[ -n "${ID_TOKEN}" ]] || fail "Cloud Run用のIDトークンを取得できませんでした。" \
  "" \
  "一般ユーザーのIDトークンでは発行先（audience）がCloud Run URLにならないため、" \
  "実行用サービスアカウント経由で発行します。" \
  "確認するもの:" \
  " ・gcloud auth list で本人のアカウントが有効か" \
  " ・${SERVICE_ACCOUNT} が存在するか" \
  " ・IAM Service Account Credentials APIが有効か" \
  " ・本人に roles/iam.serviceAccountOpenIdTokenCreator があるか" \
  "権限が無ければ deploy.sh を再実行してください（同じ設定は重複しません）。"

APP_TOKEN="$(gcloud secrets versions access latest \
  --project="${GCP_PROJECT_ID}" --secret=DASHBOARD_API_TOKEN 2>/dev/null || true)"
[[ -n "${APP_TOKEN}" ]] || fail "DASHBOARD_API_TOKEN を Secret Manager から読めませんでした。" \
  "" "→ bash scripts/cloud_run/bootstrap_secrets.sh DASHBOARD_API_TOKEN で登録してください。"

echo "URL: ${SERVICE_URL}"
echo

echo "── 1. /openapi.json ───────────────────────────────────────────"
if ! curl --silent --show-error --fail-with-body --max-time 30 \
     --config <(printf 'header = "X-Serverless-Authorization: Bearer %s"\nheader = "Authorization: Bearer smoke-test-no-app-auth"\n' "${ID_TOKEN}") \
     "${SERVICE_URL}/openapi.json" >/dev/null; then
  fail "/openapi.json が返りませんでした。ここで止まるのはアプリより手前の問題です。" \
       "" \
       "見分け方:" \
       " 403/404 が出た → 実行用サービスアカウントに呼び出し権限が無い。次を実行:" \
       "   gcloud run services add-iam-policy-binding ${CLOUD_RUN_SERVICE} \\" \
       "     --region=${GCP_REGION} --member=\"serviceAccount:${SERVICE_ACCOUNT}\" \\" \
       "     --role=roles/run.invoker" \
       " 500/503 が出た → コンテナが起動していない。ログを見る:" \
       "   gcloud run services logs read ${CLOUD_RUN_SERVICE} --region=${GCP_REGION} --limit=50" \
       " 応答なし       → デプロイが終わっていない可能性。gcloud run services list"
fi
echo
echo

echo "── 2. /api/diagnostics/integrations (postgres, advisory_lock) ──"
RESPONSE_FILE="$(mktemp "${TMPDIR:-/tmp}/crm-sfa-smoke.XXXXXX")"
trap 'rm -f "${RESPONSE_FILE}"' EXIT
if ! curl --silent --show-error --fail-with-body --max-time 60 \
     --config <(printf 'header = "X-Serverless-Authorization: Bearer %s"\nheader = "Authorization: Bearer %s"\n' "${ID_TOKEN}" "${APP_TOKEN}") \
     "${SERVICE_URL}/api/diagnostics/integrations?only=postgres,advisory_lock" \
     --output "${RESPONSE_FILE}"; then
  python3 -m json.tool --no-ensure-ascii "${RESPONSE_FILE}" 2>/dev/null || true
  fail "診断エンドポイントが返りませんでした。" \
       "" \
       " 401 が出た → 最新リビジョンにDASHBOARD_API_TOKENが設定されているか、" \
       "              Secret更新後に再デプロイしたか確認する" \
       " それ以外   → ログを見る:" \
       "   gcloud run services logs read ${CLOUD_RUN_SERVICE} --region=${GCP_REGION} --limit=50"
fi
python3 -m json.tool --no-ensure-ascii "${RESPONSE_FILE}" || \
  fail "診断エンドポイントの応答がJSONではありません。"

if ! python3 - "${RESPONSE_FILE}" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as response_file:
    report = json.load(response_file)

expected = {"postgres", "advisory_lock"}
actual = set(report.get("ok", []))
passed = (
    expected <= actual
    and not report.get("failed")
    and not report.get("not_configured")
)
raise SystemExit(0 if passed else 1)
PY
then
  fail "診断は応答しましたが、合格条件を満たしていません。" \
       "" \
       "合格条件: ok に postgres / advisory_lock の両方があり、" \
       "          failed / not_configured が空であること。" \
       "failedにある   → Neonの到達性・IP制限を確認する" \
       "not_configured  → Cloud RunのSecret設定を確認する"
fi
echo

cat <<'NEXT'
── 合格 ────────────────────────────────────────────────────────
  postgres / advisory_lock はどちらも正常です。
NEXT
