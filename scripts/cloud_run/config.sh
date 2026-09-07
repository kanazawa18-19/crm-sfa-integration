#!/usr/bin/env bash
# Cloud Run 関連スクリプトが共通で読む設定（2026-09-07、Cloud Run移行の第1段）。
#
# ここに秘密の値は書かない。GCPプロジェクトIDとリージョン・サービス名だけ。
# 実際の認証情報は Secret Manager に置き、Cloud Run からは参照名で読む
# （scripts/cloud_run/bootstrap_secrets.sh 参照）。

set -euo pipefail

# 環境変数で上書きできるようにしておく（別プロジェクトへ試し打ちしたいとき用）。
: "${GCP_PROJECT_ID:=}"
: "${GCP_REGION:=us-east4}"   # 北バージニア。Neon が AWS us-east-1（北バージニア）にあり、そのすぐ隣に置くため
#                              （旧: asia-northeast1。「日本から叩くため」というコメントは誤りだった。
#                               接続先は ep-...c-12.us-east-1.aws.neon.tech で、DBは米国東部にある）
: "${CLOUD_RUN_SERVICE:=crm-sfa-backend}"

# プロジェクトIDは gcloud の既定値から拾う（未指定なら）。
if [[ -z "${GCP_PROJECT_ID}" ]]; then
  GCP_PROJECT_ID="$(gcloud config get-value project 2>/dev/null || true)"
fi

require_gcloud() {
  if ! command -v gcloud >/dev/null 2>&1; then
    echo "ERROR: gcloud が見つかりません。" >&2
    echo "  → https://cloud.google.com/sdk/docs/install-sdk から入れて 'gcloud auth login' を実行してください。" >&2
    exit 1
  fi
  if [[ -z "${GCP_PROJECT_ID}" ]]; then
    echo "ERROR: GCPプロジェクトIDが決まりません。" >&2
    echo "  → 'gcloud config set project <PROJECT_ID>' か GCP_PROJECT_ID=... を指定してください。" >&2
    exit 1
  fi
}
