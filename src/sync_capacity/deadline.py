"""呼出し全体の絶対期限を、SDKと試験ガードまで共有する。"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
import math
import time


# 既存outboxの240秒と配備先300秒に合わせる。実測値ではない。
STORE_SECONDS = 30.0
WORKER_SECONDS = 300.0
EXECUTION_SECONDS = 240.0
FINISH_SECONDS = 10.0


class CapacityDeadlineExceeded(TimeoutError):
    """新しい処理に必要な時間が残っていない。保存結果を意味しない。"""


@dataclass(frozen=True)
class Deadline:
    expires_at: float
    # 未実測の開始許可予算。読取り2回・保存1回とガード読取り等3回分。
    # RPC上限10秒とは別で、各RPCは開始時の残時間まで短縮する。
    rpc_minimum: float = 0.05
    attempt_minimum: float = 0.3

    def __post_init__(self):
        if not all(math.isfinite(v) for v in
                   (self.expires_at, self.rpc_minimum, self.attempt_minimum)) or not (
                       0 < self.rpc_minimum and 6 * self.rpc_minimum <= self.attempt_minimum + 1e-12):
            raise ValueError("invalid deadline budget")

    @classmethod
    def after(cls, seconds=STORE_SECONDS, **kwargs):
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("invalid total budget")
        return cls(time.monotonic() + seconds, **kwargs)

    def remaining(self):
        return self.expires_at - time.monotonic()

    def require(self, minimum=None):
        remaining = self.remaining()
        if remaining < (self.rpc_minimum if minimum is None else minimum):
            raise CapacityDeadlineExceeded("capacity total deadline insufficient")
        return remaining

    def timeout(self, cap):
        return min(cap, self.require())

    def sleep(self, seconds, *, reserve=0.0):
        self.require(seconds + max(reserve, self.rpc_minimum))
        time.sleep(seconds)
        self.require(max(reserve, self.rpc_minimum))


current_deadline = ContextVar("capacity_deadline", default=None)


def effective_deadline(deadline):
    """親の短い期限と厳しい開始予算を、呼出し前の判定にも適用する。"""
    parent = current_deadline.get()
    if parent is None:
        return deadline
    return Deadline(min(parent.expires_at, deadline.expires_at),
                    max(parent.rpc_minimum, deadline.rpc_minimum),
                    max(parent.attempt_minimum, deadline.attempt_minimum))


@contextmanager
def using_deadline(deadline):
    deadline = effective_deadline(deadline)
    token = current_deadline.set(deadline)
    try:
        yield deadline
    finally:
        current_deadline.reset(token)


def budgeted(method):
    @wraps(method)
    def call(self, *args, deadline=None, **kwargs):
        deadline = deadline or current_deadline.get() or Deadline.after(self.total_seconds)
        with using_deadline(deadline):
            return method(self, *args, **kwargs)
    return call
