"""永続claimを取得してから依存を作る。実行結果の曖昧さを失敗再送で隠さない。"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Callable, Any

from src.sync_capacity.domain import Claim, JobStore, result_state

logger = logging.getLogger(__name__)


def drain_one(store: JobStore, prepare: Callable[[Claim], Callable[[], tuple[dict, bool]]],
              *, clock: Callable[[], float] = time.time) -> dict[str, Any]:
    # claimの応答が失われたときは処理を始めない。保存済みownerを手動照合する。
    owner = uuid.uuid4().hex
    try:
        claim = store.claim(owner, clock())
    except Exception:
        logger.warning("sync_capacity claim_unconfirmed owner=%s", owner)
        raise
    if claim is None:
        return {"state": "idle_or_full"}
    logger.info("sync_capacity processing job_id=%s owner=%s slot=%s",
                claim.job_id, claim.owner, claim.slot)
    try:
        execute = prepare(claim)
    except Exception:
        # 依存の組立だけ失敗。業務処理をまだ呼んでいないので再試行できる。
        store.finish(claim, "retry", "initialization_failed", clock())
        logger.info("sync_capacity retry job_id=%s owner=%s", claim.job_id, claim.owner)
        return {"job_id": claim.job_id, "state": "retry"}
    try:
        result, partial = execute()
        state, reason = result_state(result, partial=partial)
    except Exception:
        state, reason = "needs_attention", "execution_failed"
    # SystemExit/強制終了ではここに来ない。枠は期限なしで保持される。
    # finish応答不明時に別のrelease操作を行わない（commit済みかもしれない）。
    try:
        store.finish(claim, state, reason, clock())
    except Exception:
        logger.warning("sync_capacity finish_unconfirmed job_id=%s owner=%s", claim.job_id, claim.owner)
        raise
    logger.info("sync_capacity %s job_id=%s owner=%s", state, claim.job_id, claim.owner)
    return {"job_id": claim.job_id, "state": state}
