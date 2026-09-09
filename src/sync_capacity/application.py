"""永続claimを取得してから依存を作る。実行結果の曖昧さを失敗再送で隠さない。"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Callable, Any

from src.sync_capacity.domain import Claim, JobStore, result_state
from src.sync_capacity.telemetry import emit


def drain_one(store: JobStore, prepare: Callable[[Claim], Callable[[], tuple[dict, bool]]],
              *, clock: Callable[[], float] = time.time,
              monotonic: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    owner = uuid.uuid4().hex
    started = monotonic()
    fields = {"owner": owner}

    def record(event, **values):
        # 本文・例外・業務結果を渡さない。未開始の時間はnullのまま残す。
        warning = (event.endswith("unconfirmed") or event == "initialization_failed"
                   or values.get("state") == "needs_attention")
        emit(event, level=logging.WARNING if warning else logging.INFO, **fields, **values)

    record("claim_started")
    claiming = monotonic()
    try:
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
    try:
        execute = prepare(claim)
    except Exception:
        initialization_seconds = monotonic() - initializing
        state, reason = "retry", "initialization_failed"
        record("initialization_failed", initialization_seconds=initialization_seconds)
    else:
        initialization_seconds = monotonic() - initializing
        record("initialization_finished", initialization_seconds=initialization_seconds)
        execution_started = monotonic()
        try:
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
    record("finish_started", state=state, reason=reason, **timings)
    saving = monotonic()
    try:
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
