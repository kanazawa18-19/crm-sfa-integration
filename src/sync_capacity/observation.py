"""本文を取得せず全件を走査する。失敗時に部分件数を成功として返さない。"""

import math
import time

STATES = ("pending", "retry", "processing", "completed", "needs_attention")


def observe_queue(jobs, *, clock=time.time, monotonic=time.monotonic):
    started_at, started = clock(), monotonic()
    counts = dict.fromkeys(STATES, 0)
    invalid_states = 0
    invalid_waiting_timestamps = 0
    oldest = None
    # order_by(created_at)は時刻欠落文書を除外するので使わない。
    for snapshot in jobs.select(["state", "created_at"]).stream():
        data = snapshot.to_dict()
        state = data.get("state")
        if state not in counts:
            invalid_states += 1
            continue
        counts[state] += 1
        if state in {"pending", "retry"}:
            created = data.get("created_at")
            if (isinstance(created, bool) or not isinstance(created, (float, int))
                    or not math.isfinite(created) or created > started_at):
                invalid_waiting_timestamps += 1
            else:
                oldest = created if oldest is None else min(oldest, created)
    ended_at = clock()
    return {"scan_started_at": started_at, "scan_finished_at": ended_at,
            "scan_seconds": monotonic() - started, "complete": True,
            "consistency": "full_scan_not_atomic_snapshot", "counts": counts,
            "total": sum(counts.values()) + invalid_states,
            "unknown_state_count": invalid_states,
            "invalid_waiting_timestamp_count": invalid_waiting_timestamps,
            "oldest_waiting_created_at": oldest if not invalid_waiting_timestamps else None,
            "oldest_waiting_age_seconds": (started_at - oldest if oldest is not None
                                            and not invalid_waiting_timestamps else None),
            "waiting_age_basis": "since_original_acceptance_including_retry_delay"}
