#!/usr/bin/env bash
# Cloud Run の 404 が「アプリ側か・プロジェクト側か・組織側か」を切り分ける（2026-09-07）
# 自作イメージではなく Google 公式の hello コンテナを使う。ビルドもシークレットも不要。
set -uo pipefail

HELLO="us-docker.pkg.dev/cloudrun/container/hello"
REGION="us-east4"
PROJ_A="fabled-electron-406310"   # いま404が出ているプロジェクト
PROJ_B="valid-dragon-402709"      # 空の既定プロジェクト（比較用）
BILLING="01EA6F-556121-34B9A6"
SVC="hello-test"

RES_A="未実施"; RES_B="未実施"

probe() {  # $1=URL → 最大3分、15秒おきに叩いて最後のコードを返す
  local url="$1" code=""
  for _ in $(seq 1 12); do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "$url" 2>/dev/null)"
    [[ "$code" == "200" ]] && { echo "$code"; return; }
    sleep 15
  done
  echo "$code"
}

echo "################ テストA：同じプロジェクトに公式helloを出す ################"
gcloud services enable run.googleapis.com --project="$PROJ_A" --quiet 2>&1 | tail -2
if gcloud run deploy "$SVC" --image="$HELLO" --region="$REGION" \
     --allow-unauthenticated --project="$PROJ_A" --quiet 2>&1 | tail -6; then
  URL_A="$(gcloud run services describe "$SVC" --region="$REGION" --project="$PROJ_A" \
            --format='value(status.url)' 2>/dev/null)"
  echo "URL_A = $URL_A"
  RES_A="$(probe "$URL_A")"
  echo "→ テストA の応答: $RES_A"
else
  RES_A="デプロイ失敗"
fi

if [[ "$RES_A" == "200" ]]; then
  echo
  echo "################ テストAが通ったのでBは省略 ################"
else
  echo
  echo "################ テストB：別プロジェクトに公式helloを出す ################"
  gcloud billing projects link "$PROJ_B" --billing-account="$BILLING" --quiet 2>&1 | tail -2
  gcloud services enable run.googleapis.com --project="$PROJ_B" --quiet 2>&1 | tail -2
  if gcloud run deploy "$SVC" --image="$HELLO" --region="$REGION" \
       --allow-unauthenticated --project="$PROJ_B" --quiet 2>&1 | tail -6; then
    URL_B="$(gcloud run services describe "$SVC" --region="$REGION" --project="$PROJ_B" \
              --format='value(status.url)' 2>/dev/null)"
    echo "URL_B = $URL_B"
    RES_B="$(probe "$URL_B")"
    echo "→ テストB の応答: $RES_B"
  else
    RES_B="デプロイ失敗"
  fi
fi

echo
echo "==================== 結果 ===================="
printf "  A 同じプロジェクト %-22s : %s\n" "$PROJ_A" "$RES_A"
printf "  B 別プロジェクト   %-22s : %s\n" "$PROJ_B" "$RES_B"
echo "----------------------------------------------"
if   [[ "$RES_A" == "200" ]]; then
  echo "  判定: プロジェクトは正常。原因は crm-sfa 側のサービス設定にある"
elif [[ "$RES_B" == "200" ]]; then
  echo "  判定: $PROJ_A 固有の問題。別プロジェクトへ移せば進める"
elif [[ "$RES_A" == "404" && "$RES_B" == "404" ]]; then
  echo "  判定: 組織 cnctor.jp 全体で run.app が公開されていない。管理者案件"
else
  echo "  判定: 上の出力をそのまま貼って相談すること"
fi
echo "=============================================="
echo
echo "片付け（結果を報告した後でよい）:"
echo "  gcloud run services delete $SVC --region=$REGION --project=$PROJ_A --quiet"
echo "  gcloud run services delete $SVC --region=$REGION --project=$PROJ_B --quiet"
echo "  gcloud billing projects unlink $PROJ_B"
