"""永続claimを取得してから依存を作る。実行結果の曖昧さを失敗再送で隠さない。"""

from __future__ import annotations

import logging
import math
import time
import uuid
from typing import Callable, Any

from src.sync_capacity.domain import Claim, JobStore, result_state
from src.sync_capacity.telemetry import emit
from src.sync_capacity.deadline import (Deadline, using_deadline, effective_deadline, CapacityDeadlineExceeded,
    STORE_SECONDS, WORKER_SECONDS, EXECUTION_SECONDS, FINISH_SECONDS)


def drain_one(store: JobStore, prepare: Callable[[Claim], Callable[[], tuple[dict, bool]]],
              *, clock: Callable[[], float] = time.time,
              monotonic: Callable[[], float] = time.monotonic,
              deadline: Deadline | None = None, execution_budget: float = EXECUTION_SECONDS,
              finish_budget: float = FINISH_SECONDS) -> dict[str, Any]:
    deadline = effective_deadline(deadline or Deadline.after(WORKER_SECONDS))
    if not all(math.isfinite(v) and v > 0 for v in (execution_budget, finish_budget)):
        raise ValueError("invalid drain stage budget")
    owner = uuid.uuid4().hex
    started = monotonic()
    fields = {"owner": owner}

    def record(event, **values):
        # 本文・例外・業務結果を渡さない。未開始の時間はnullのまま残す。
        warning = (event.endswith("unconfirmed") or event in {"initialization_failed", "initialization_not_started", "finish_not_started"}
                   or values.get("state") == "needs_attention")
        emit(event, level=logging.WARNING if warning else logging.INFO, **fields, **values)

    record("claim_started")
    claiming = monotonic()
    try:
        deadline.require(execution_budget + finish_budget + deadline.attempt_minimum)
        claim_deadline = Deadline(min(deadline.expires_at - execution_budget - finish_budget,
                                      time.monotonic() + STORE_SECONDS),
                                  deadline.rpc_minimum, deadline.attempt_minimum)
        with using_deadline(claim_deadline):
            claim = store.claim(owner, clock())
    except Exception:
        record("claim_unconfirmed", claim_seconds=monotonic() - claiming)
        raise
    claimed = monotonic()
    if claim is None:
        record("idle_or_full", claim_seconds=claimed - claiming)
        return {"state": "idle_or_full"}
    fields.update(job_id=claim.job_id, source=claim.source, owner=claim.owner, slot=claim.slot)
    record("processing", claim_seconds=claimed - claiming)
    initialization_seconds = None
    execution_seconds = None
    initializing = monotonic()
    initialization_started = False
    try:
        deadline.require(execution_budget + finish_budget)
        initialization_started = True
        execute = prepare(claim)
    except Exception:
        initialization_seconds = monotonic() - initializing if initialization_started else None
        state = "retry"
        reason = "initialization_failed" if initialization_started else "initialization_not_started"
        record(reason, initialization_seconds=initialization_seconds)
    else:
        initialization_seconds = monotonic() - initializing
        record("initialization_finished", initialization_seconds=initialization_seconds)
        execution_started = monotonic()
        try:
            deadline.require(execution_budget + finish_budget)
        except CapacityDeadlineExceeded:
            state, reason = "retry", "execution_not_started"
        else:
            execution_deadline = Deadline(deadline.expires_at - finish_budget,
                                          deadline.rpc_minimum, deadline.attempt_minimum)
            try:
                with using_deadline(execution_deadline):
                    result, partial = execute()
                state, reason = result_state(result, partial=partial)
            except Exception:
                state, reason = "needs_attention", "execution_failed"
            execution_seconds = monotonic() - execution_started
        record("execution_finished", state=state, reason=reason, execution_seconds=execution_seconds)
    # 強制終了時は終了ログが欠測になり、枠を保持する。finishは1回だけ呼ぶ。
    timings = {"claim_seconds": claimed - claiming,
               "initialization_seconds": initialization_seconds,
               "execution_seconds": execution_seconds}
    try:
        deadline.require(finish_budget)
    except CapacityDeadlineExceeded:
        record("finish_not_started", intended_state=state, reason=reason, **timings,
               finish_seconds=None, attempt_seconds=monotonic() - started)
        raise
    record("finish_started", state=state, reason=reason, **timings)
    saving = monotonic()
    try:
        with using_deadline(deadline):
            store.finish(claim, state, reason, clock())
    except Exception:
        ended = monotonic()
        record("finish_unconfirmed", intended_state=state, reason=reason, **timings,
               finish_seconds=ended - saving, attempt_seconds=ended - started)
        raise
    ended = monotonic()
    record("finished", state=state, reason=reason, **timings,
           finish_seconds=ended - saving, attempt_seconds=ended - started)
    return {"job_id": claim.job_id, "state": state}
