# crm-sfa-integration のバックエンド（FastAPI）を Cloud Run で動かすためのイメージ。
#
# なぜコンテナにするか（2026-09-07、Cloud Run移行の第1段）:
#   1. Vercelの関数は maxDuration 300秒固定で、長いバッチが途中で死ぬ
#   2. Vercel Hobby の cron は最大1時間ずれる（定時実行にならない）
#   3. Vercel Hobby のログ保持が1時間しかなく、後追いで原因を追えない
# Cloud Run はリクエストタイムアウト最大60分・Cloud Scheduler は分単位で正確・
# ログは Cloud Logging に既定30日残るため、この3点がまとめて解ける。
#
# Vercel側の api/index.py は残す。Webhookの受け口（kintone/Zoho/Notionに登録済みのURL）は
# Vercelのままにして中継する方針のため、このイメージと api/index.py は同じ
# `src.api.app:app` を指す（＝実装は二重化しない）。

# VercelのPythonランタイムと同じ 3.12 系に揃える。今どちらで動いても挙動が変わらないようにする。
FROM python:3.12-slim

# PYTHONDONTWRITEBYTECODE: .pyc を書かない（読み取り専用FSでの書き込みを減らす）
# PYTHONUNBUFFERED:        print/loggingを即座に Cloud Logging へ流す（バッファに溜めない）
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app

WORKDIR /app

# 依存だけを先に入れて層をキャッシュする（srcを変えただけの再ビルドでpipが走らない）。
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 実行時に必要なものだけを入れる。tests/ dashboard/ docs/ scripts/ gas/ は .dockerignore で除外。
COPY src/ ./src/
COPY config/ ./config/

# 非rootで動かす。Cloud Runは任意のUIDで起動できるので、書き込みが要る場所を作らない前提。
RUN useradd --create-home --uid 10001 appuser
USER appuser

# Cloud Run は $PORT を注入する（既定8080）。ローカルで docker run するときの既定値も同じにする。
ENV PORT=8080
EXPOSE 8080

# exec形式でシェルを1枚だけ挟む。exec を付けないと uvicorn が PID 1 にならず、
# Cloud Run が送る SIGTERM を受け取れずに強制終了になる（graceful shutdown が効かない）。
CMD ["sh", "-c", "exec uvicorn src.api.app:app --host 0.0.0.0 --port ${PORT:-8080} --timeout-keep-alive 65"]
