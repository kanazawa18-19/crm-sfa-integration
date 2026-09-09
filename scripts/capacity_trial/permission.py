"""observerの固定合成書込みだけをサーバへ送り、IAMの拒否を検証する。"""
from uuid import uuid4

from .guard import BASE, Refused


def probe(store, ledger):
    from google.api_core.exceptions import PermissionDenied
    from google.cloud.firestore_v1.types import Write, Document
    from google.cloud.firestore_v1 import _helpers
    from src.sync_capacity.domain import submission
    from .firestore import GuardedAPI
    item = submission("notion", {"id": "synthetic-smoke"}, None, 0)
    smoke = store.client.document("sync_capacity_scopes/trial-smoke/jobs/" + item.job_id).get()
    if not smoke.exists or smoke.to_dict().get("event") != item.event:
        raise Refused("合成smoke文書の読取確認ができません")
    guarded = store.client._firestore_api_internal
    if guarded.role != "observer":
        raise Refused("IAM試験はobserverだけを許可")
    # 一般のobserver書込みガードは維持し、この固定createだけ予算ガードを通して送る。
    name = BASE + "/documents/sync_capacity_scopes/trial-permission/jobs/synthetic-write-denied"
    write = Write(update=Document(name=name, fields=_helpers.encode_dict({
        "event": {"id": "synthetic-permission-probe"}})), current_document={"exists": False})
    marker = "observer_write_pending:" + uuid4().hex
    # 送信前に停止を永続化し、強制終了でも未確認の権限で続行させない。
    def mark_pending():
        ledger.transact(lambda data: data.update(halted=marker))
    budgeted = GuardedAPI(guarded.api, ledger, role="runner", before_commit=mark_pending)
    try:
        budgeted.commit(request={"database": BASE, "writes": [write]})
    except PermissionDenied as exc:
        def clear_pending(data):
            if data.get("halted") != marker:
                raise Refused("IAM試験の停止記録が一致しません")
            data.pop("halted")
        ledger.transact(clear_pending)
        return {"read_verified": True, "write_denied_by_server": True, "server_code": int(exc.code),
                "attempted_document": name}
    # 書けた場合は合成1件でも権限不備。以後の試験も明示停止する。
    ledger.transact(lambda data: data.update(halted="observer_write_succeeded"))
    raise Refused("observer書込みが成功したため以後停止")
