FROM python:3.12-slim
WORKDIR /app
COPY scripts/cloud_run/gmail_trial/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY src/gmail_sync/watch_result.py /app/src/gmail_sync/watch_result.py
COPY src/infrastructure/cron_result_log.py /app/src/infrastructure/cron_result_log.py
COPY scripts/cloud_run/gmail_trial/app.py /app/app.py
RUN python -c 'import app'
USER 65532:65532
CMD exec uvicorn app:app --host 0.0.0.0 --port ${PORT:-8080} --no-access-log
