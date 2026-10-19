"""認証済み通常同期の通知から、添付の再読だけを予約する。"""
import logging
import os
from uuid import uuid4
from src.attachment_sync.repository import Repository
from src.contact_name_job.repository import connect


def enqueue(db, record):
    if db not in ('project', 'action') or os.environ.get('ATTACHMENT_SYNC_ENABLED') != 'true':
        return
    try:
        with connect() as conn:
            Repository(conn).put_state('event:' + db + ':' + record,
                                       {'db': db, 'record': record, 'version': uuid4().hex})
    except Exception as exc:
        # 通常項目の同期は継続。全件巡回で回収でき、ログから受付障害を検知できる。
        logging.getLogger(__name__).warning('添付の再読予約失敗: %s', type(exc).__name__)
