#!/usr/bin/env bash
# Cloud Run に載せたバックエンドの到達確認（2026-09-07、移行の第1段）。
#
# 確かめるのは2つだけ。どちらも読み取りで、本番データを書き換えない。
#   1. GET /healthz                                  … コンテナが起きて応答するか
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

ID_TOKEN="$(gcloud auth print-identity-token 2>/dev/null || true)"
[[ -n "${ID_TOKEN}" ]] || fail "GoogleのIDトークンを取得できませんでした。" \
  "" "→ gcloud auth login をやり直してください。"

APP_TOKEN="$(gcloud secrets versions access latest \
  --project="${GCP_PROJECT_ID}" --secret=DASHBOARD_API_TOKEN 2>/dev/null || true)"
[[ -n "${APP_TOKEN}" ]] || fail "DASHBOARD_API_TOKEN を Secret Manager から読めませんでした。" \
  "" "→ bash scripts/cloud_run/bootstrap_secrets.sh DASHBOARD_API_TOKEN で登録してください。"

echo "URL: ${SERVICE_URL}"
echo

echo "── 1. /healthz ────────────────────────────────────────────────"
if ! curl --silent --show-error --fail-with-body --max-time 30 \
     --config <(printf 'header = "X-Serverless-Authorization: Bearer %s"\n' "${ID_TOKEN}") \
     "${SERVICE_URL}/healthz"; then
  fail "/healthz が返りませんでした。ここで止まるのはアプリより手前の問題です。" \
       "" \
       "見分け方:" \
       " 403 が出た   → 本人に呼び出し権限が無い。次を実行:" \
       "   gcloud run services add-iam-policy-binding ${CLOUD_RUN_SERVICE} \\" \
       "     --region=${GCP_REGION} --member=\"user:\$(gcloud config get-value account)\" \\" \
       "     --role=roles/run.invoker" \
       " 500/503 が出た → コンテナが起動していない。ログを見る:" \
       "   gcloud run services logs read ${CLOUD_RUN_SERVICE} --region=${GCP_REGION} --limit=50" \
       " 応答なし       → デプロイが終わっていない可能性。gcloud run services list"
fi
echo
echo

echo "── 2. /api/diagnostics/integrations (postgres, advisory_lock) ──"
if ! curl --silent --show-error --fail-with-body --max-time 60 \
     --config <(printf 'header = "X-Serverless-Authorization: Bearer %s"\nheader = "Authorization: Bearer %s"\n' "${ID_TOKEN}" "${APP_TOKEN}") \
     "${SERVICE_URL}/api/diagnostics/integrations?only=postgres,advisory_lock" \
     | python3 -m json.tool --no-ensure-ascii; then
  fail "診断エンドポイントが返りませんでした。" \
       "" \
       " 401 が出た → アプリ側のトークンが違う。Cloud Run に渡している" \
       "              DASHBOARD_API_TOKEN と、Vercel 側の値が同じか確認する" \
       " それ以外   → ログを見る:" \
       "   gcloud run services logs read ${CLOUD_RUN_SERVICE} --region=${GCP_REGION} --limit=50"
fi
echo

cat <<'NEXT'
── 見るところ ──────────────────────────────────────────────────
  "ok"             に postgres と advisory_lock が両方入っていれば合格
  "failed"         が空でないなら、GCPからNeonへ届いていない
                   → Neon のIP制限（Allowed IPs）を確認する。GCPのIPは未許可のはず
  "not_configured" に出るなら、シークレットがCloud Runに渡っていない
                   → gcloud run services describe で --set-secrets を確認
NEXT
