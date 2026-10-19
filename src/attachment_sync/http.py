"""容量を制限したHTTP。任意URLの転送・自動リダイレクト・POST再試行は行わない。"""
import requests
import time
from contextvars import ContextVar
from contextlib import contextmanager

_deadline = ContextVar("attachment_deadline", default=None)


def check_budget():
    if _deadline.get() is not None and time.monotonic() >= _deadline.get():
        raise TimeoutError("添付の処理時間を超えました")


@contextmanager
def budget(seconds=180):
    token = _deadline.set(time.monotonic() + seconds)
    try: yield
    finally: _deadline.reset(token)

from src.attachment_sync.domain import Held, MAX_BYTES


class RemoteError(RuntimeError):
    pass


def request(method, url, accepted=None, **kwargs):
    check_budget()
    response = requests.request(method, url, timeout=(10, 30), allow_redirects=False, **kwargs)
    if not (response.status_code in accepted if accepted else 200 <= response.status_code < 300):
        response.close()
        # URL、応答本文、トークンを例外へ混ぜない。
        raise RemoteError('添付API HTTP ' + str(response.status_code))
    return response


def download(url, **kwargs):
    with request('GET', url, stream=True, **kwargs) as response:
        if int(response.headers.get('Content-Length') or 0) > MAX_BYTES:
            raise Held('添付は10MBを超えています')
        chunks, size = [], 0
        for chunk in response.iter_content(65536):
            check_budget()
            size += len(chunk)
            if size > MAX_BYTES:
                raise Held('添付は10MBを超えています')
            chunks.append(chunk)
        return b''.join(chunks)


def require_budget(seconds):
    if _deadline.get() is not None and _deadline.get() - time.monotonic() < seconds:
        raise TimeoutError('添付の保存に必要な時間が不足しています')
