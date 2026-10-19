"""サーバーで指定した試験用原本だけ、同期に伴う共有通知を抑える。"""
import os
from contextlib import contextmanager
from contextvars import ContextVar

_isolated = ContextVar('crm_test_isolated', default=False)


def is_test_record(page_id):
    allowed = {v.strip().replace('-', '') for v in os.environ.get('CRM_TEST_NOTION_IDS', '').split(',') if v.strip()}
    return str(page_id).replace('-', '') in allowed


def notifications_suppressed():
    return _isolated.get()


@contextmanager
def test_record_scope(page_id):
    token = _isolated.set(is_test_record(page_id))
    try:
        yield
    finally:
        _isolated.reset(token)
