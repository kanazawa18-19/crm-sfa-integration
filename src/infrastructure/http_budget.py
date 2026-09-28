"""複数のHTTP呼出し・認証更新で共有する実行時間。未設定の既存経路は変えない。"""
from contextlib import contextmanager
from contextvars import ContextVar
import time

_expires = ContextVar('http_budget_expires', default=None)


class HttpBudgetExceeded(TimeoutError):
    """次の通信・待機を始められる時間がない。"""


def remaining():
    expires = _expires.get()
    if expires is None:
        return None
    value = expires - time.monotonic()
    if value <= 0:
        raise HttpBudgetExceeded('HTTP処理の時間予算に到達しました')
    return value


@contextmanager
def http_budget(seconds):
    parent = _expires.get()
    expires = time.monotonic() + seconds
    token = _expires.set(min(parent, expires) if parent is not None else expires)
    try:
        yield
    finally:
        _expires.reset(token)
