"""実commit成功後の応答だけを遮断し、再送されないことと保存状態を読む。"""
import time

from .guard import Refused


class LostResponse(TimeoutError):
    """この試験がcommit成功を確認した後だけ送出する専用例外。"""


class DropCommitResponse:
    def __init__(self, guarded):
        self.guarded = guarded
        self.calls = 0
        self.committed = 0

    def __getattr__(self, name):
        return getattr(self.guarded, name)

    def commit(self, *args, **kwargs):
        self.calls += 1
        self.guarded.commit(*args, **kwargs)
        self.committed += 1
        raise LostResponse("合成のcommit保存後応答喪失")


def response_loss(store):
    from src.sync_capacity.domain import submission
    operation = store.settings.scope.removeprefix("trial-loss-")
    if store.scope_ref.get().exists or list(store.jobs.limit(1).stream()):
        raise Refused("応答喪失scopeは使用済み")
    now = time.time()
    item = submission("notion", {"id": "synthetic-response-loss"}, None, now)
    claim = None
    if operation != "initialize":
        store.initialize(apply=True)
    if operation in {"claim", "finish", "recover"}:
        store.enqueue(item, now)
    if operation in {"finish", "recover"}:
        claim = store.claim("synthetic-loss-owner", now)
        if claim is None:
            raise AssertionError("事前の枠取得失敗")
    actions = {
        "initialize": lambda: store.initialize(apply=True),
        "enqueue": lambda: store.enqueue(item, now),
        "claim": lambda: store.claim("synthetic-loss-owner", now),
        "finish": lambda: store.finish(claim, "completed", "synthetic", now),
        # claimだけを同期呼出しし、業務workerは一切起動していない。
        "recover": lambda: store.recover(item.job_id, claim.owner, now=now, apply=True, stopped=True),
    }
    if operation not in actions:
        raise Refused("応答喪失操作が不正")
    original = store.client._firestore_api_internal
    drop = DropCommitResponse(original)
    store.client._firestore_api_internal = drop
    try:
        try:
            actions[operation]()
        except LostResponse:
            pass
        else:
            raise AssertionError("保存後の応答喪失が伝播しなかった")
    finally:
        store.client._firestore_api_internal = original
    if drop.calls != 1 or drop.committed != 1:
        raise AssertionError("結果不明commitが再送された")
    scope = store.scope_ref.get().to_dict()
    jobs = list(store.jobs.limit(2).stream())
    expected = {"enqueue": "pending", "claim": "processing", "finish": "completed", "recover": "needs_attention"}
    if operation == "initialize":
        if jobs or scope["limit"] != 3 or any(scope["slots"].values()):
            raise AssertionError("初期化保存状態の不一致")
    else:
        if len(jobs) != 1 or jobs[0].id != item.job_id or jobs[0].to_dict()["state"] != expected[operation]:
            raise AssertionError("保存jobの件数・状態の不一致")
        job = jobs[0].to_dict()
        if operation == "claim":
            if scope["slots"].get(job["slot"]) != {"job_id": item.job_id, "owner": "synthetic-loss-owner"}:
                raise AssertionError("結果不明claimの枠が保持されていない")
        elif any(scope["slots"].values()):
            raise AssertionError("不要な枠が残っている")
    return {"operation": operation, "commit_calls": drop.calls, "committed": drop.committed,
            "response_loss_injected": True, "saved_state_verified": True,
            "job_count": len(jobs), "slots_retained": operation == "claim"}
