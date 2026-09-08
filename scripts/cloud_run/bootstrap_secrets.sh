#!/usr/bin/env bash
# Cloud Run が読む認証情報を Secret Manager へ登録する（2026-09-07、Cloud Run移行の第1段）。
#
# なぜファイルやコマンド引数で渡さないか:
#   - リポジトリにも ~/notes にも認証情報は置かない決まりのため
#   - `gcloud ... --data-file=-` に標準入力で渡せば、値がシェル履歴にも ps にも残らない
#
# 使い方:
#   pbpaste | bash scripts/cloud_run/bootstrap_secrets.sh DATABASE_URL   ★おすすめ
#   bash scripts/cloud_run/bootstrap_secrets.sh DATABASE_URL             （手入力）
#   bash scripts/cloud_run/bootstrap_secrets.sh --list
#
# ★ おすすめは「先に値をコピーしてから pbpaste で流し込む」形。
#   画面にもシェル履歴にも ps にも残らない。
#   手入力の場合もエコーは止めてあるが、クリップボード経由のほうが確実。
#
# 現在の deploy.sh（第2段対応版）が必要とするのは次の5つ:
#   DATABASE_URL / DATABASE_URL_UNPOOLED / DASHBOARD_API_TOKEN
#   TOKEN_ENCRYPTION_KEY / SLACK_WEBHOOK_URL_ALERT
# 先頭3つは第1段で登録済み。後ろ2つは最初に移すcron
# （token-encryption-healthcheck）が読む値で、2026-09-08に追加した。
#
# ★ CRON_SECRET は要らなくなった（2026-09-08）。Cloud SchedulerはOIDCで認証するため、
#   Cloud Run側にVercelの合言葉を置く意味が無い。詳しくは deploy.sh のコメント。
#
# ★ Vercel 側の値は読み戻せない（Sensitive指定は画面もCLIもプレースホルダを返す）。
#   Neonのダッシュボードなど、発行元から取り直すこと。

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/cloud_run/config.sh
source "${SCRIPT_DIR}/config.sh"
require_gcloud

if [[ "${1:-}" == "--list" ]]; then
  gcloud secrets list --project="${GCP_PROJECT_ID}" --format="table(name, createTime)"
  exit 0
fi

SECRET_NAME="${1:-}"
if [[ -z "${SECRET_NAME}" ]]; then
  echo "使い方: bash scripts/cloud_run/bootstrap_secrets.sh <SECRET_NAME>" >&2
  echo "        bash scripts/cloud_run/bootstrap_secrets.sh --list" >&2
  exit 1
fi

echo "プロジェクト: ${GCP_PROJECT_ID}"
echo "シークレット名: ${SECRET_NAME}"

# 値の受け取り方を2つに分ける。
#   パイプ（pbpaste | ...）  … 標準入力をそのまま読む
#   手入力                   … read -s でエコーを止めて1行読む
# どちらも最終的に gcloud へは標準入力で渡す。コマンド引数には載せないので ps に出ない。
if [[ -t 0 ]]; then
  read -rs -p "値を貼り付けて Enter（画面には出ません）: " SECRET_VALUE
  echo
else
  SECRET_VALUE="$(cat)"
fi

# 末尾の改行はシークレットの値としては邪魔になる（pbpaste や echo が付ける）ので落とす。
SECRET_VALUE="${SECRET_VALUE%$'\n'}"

if [[ -z "${SECRET_VALUE}" ]]; then
  echo "ERROR: 値が空です。何も登録していません。" >&2
  echo "  → 値をコピーしてから 'pbpaste | bash scripts/cloud_run/bootstrap_secrets.sh ${SECRET_NAME}' を試してください。" >&2
  exit 1
fi

echo "受け取った値: ${#SECRET_VALUE} 文字（中身は表示しません）"

# 既存なら版を足す、無ければ作る。どちらも標準入力から読ませる。
#
# ★ describe の失敗を「無い」と決めつけない（2026-09-08）。
#   認証切れでも describe は失敗するので、以前の書き方だと
#   「シークレットが無い」と誤診して create へ進んでいた。
#   実際に踏んだ：Reauthentication failed → create を呼びに行った。
#   NOT_FOUND のときだけ新規作成し、それ以外の失敗はここで止める。
#   ★ set -e を一時的に外す。外さないと、describe が失敗した瞬間に
#     スクリプトが無言で終わり、下の判定に到達しない（2026-09-08 に実際に踏んだ）。
set +e
DESCRIBE_ERR="$(gcloud secrets describe "${SECRET_NAME}" \
  --project="${GCP_PROJECT_ID}" 2>&1 >/dev/null)"
DESCRIBE_STATUS=$?
set -e

if [[ ${DESCRIBE_STATUS} -ne 0 ]] && ! grep -qiE 'NOT_FOUND|was not found' <<<"${DESCRIBE_ERR}"; then
  echo "ERROR: シークレットの有無を確認できませんでした。何も登録していません。" >&2
  # gcloud は理由を先頭に書く（末尾は「別アカウントを使うなら…」の案内）ので head を取る。
  echo "${DESCRIBE_ERR}" | head -3 >&2
  echo "  → 認証切れなら 'gcloud auth login' を実行してからやり直してください。" >&2
  exit 1
fi

if [[ ${DESCRIBE_STATUS} -eq 0 ]]; then
  printf '%s' "${SECRET_VALUE}" | gcloud secrets versions add "${SECRET_NAME}" \
    --project="${GCP_PROJECT_ID}" --data-file=-
  echo "→ 既存のシークレットに新しい版を追加しました。"
else
  printf '%s' "${SECRET_VALUE}" | gcloud secrets create "${SECRET_NAME}" \
    --project="${GCP_PROJECT_ID}" --replication-policy=automatic --data-file=-
  echo "→ 新規に作成しました。"
fi
unset SECRET_VALUE

echo
echo "★ Cloud Run のサービスアカウントに読み取り権限を付けるのを忘れないこと:"
echo "   bash scripts/cloud_run/deploy.sh が --set-secrets で参照します。"
