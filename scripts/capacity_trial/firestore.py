"""Firestore SDKのRPC直前に資源・再試行・上限を検査する。"""
from __future__ import annotations

from .guard import BASE, PROJECT, DATABASE, Refused, validate_resource, validate_target, validate_environment


class GuardedAPI:
    def __init__(self, api, ledger, *, role, before_commit=None):
        self.api, self.ledger, self.role = api, ledger, role
        self.before_commit = before_commit
        policy = ledger.upper_bound()
        self.small = isinstance(policy, dict) and policy.get("basis") == "reserved-upper-bound"

    def __getattr__(self, method):
        if method not in {"batch_get_documents", "run_query", "commit"}:
            raise Refused("未承認のFirestore RPC")

        def authorize_resource(name):
            prefix = BASE + "/documents/sync_capacity_scopes/"
            if isinstance(name, str) and name.startswith(prefix):
                self.ledger.authorize_scope(name[len(prefix):].split("/")[0])

        def call(request=None, **kwargs):
            request = dict(request or {})
            validate_resource(request.get("database", BASE))
            reads, writes = 0, 0
            document_names = []
            if self.small:
                from . import small_policy
                request = {key: value for key, value in request.items() if value is not None}
                allowed_keys = {"batch_get_documents": {"database", "documents"},
                    "run_query": {"parent", "structured_query"}, "commit": {"database", "writes"}}
                if set(request) - allowed_keys[method]:
                    raise Refused("未承認の小規模RPCオプション")
            if method == "batch_get_documents":
                refs = request.get("documents", [])
                if not refs:
                    raise Refused("文書名がない読取り")
                if self.small and len(refs) != 1:
                    raise Refused("小規模文書読取りは1文書ずつ")
                for ref in refs:
                    validate_resource(ref)
                    authorize_resource(ref)
                    if self.small:
                        small_policy.resource(ref)
                reads = len(refs)
            elif method == "run_query":
                validate_resource(request.get("parent"))
                authorize_resource(request.get("parent"))
                query = request.get("structured_query")
                from google.cloud.firestore_v1.types import StructuredQuery
                query = StructuredQuery(query)
                if len(query.from_) != 1 or query.from_[0].collection_id != "jobs" or query.from_[0].all_descendants:
                    raise Refused("jobs以外またはcollection group走査")
                # 中断時も全量を予約したままにする。返却文書数は別記録。
                if query.offset:
                    raise Refused("offsetによる未計数の読取りは禁止")
                if self.small:
                    small_policy.resource(request["parent"])
                    small_policy.query_shape(query)
                reads = int(query.limit) if query.limit else 100_001
                if not 1 <= reads <= 100_001:
                    raise Refused("走査読取り上限")
                # 最後の1件は超過検出用。上限を切った集計を全件成功にしない。
                query.limit = reads
                request["structured_query"] = query
            else:
                if self.role != "runner":
                    raise Refused("観測用プロセスの書込み禁止")
                from google.cloud.firestore_v1.types import Write
                changes = request.get("writes", [])
                if not 1 <= len(changes) <= 400:
                    raise Refused("書込みbatch上限")
                for change in changes:
                    change = Write(change)
                    if change.delete or change.transform.document or not change.update.name:
                        raise Refused("削除・単独transformは未承認")
                    validate_resource(change.update.name)
                    authorize_resource(change.update.name)
                if self.small:
                    from google.cloud.firestore_v1.types import CommitRequest
                    if len(changes) > 8 or len(CommitRequest.serialize(CommitRequest(request))) > 32768:
                        raise Refused("小規模commitの8文書・32KiB上限を超過")
                    def read_existing(name):
                        responses = list(self.batch_get_documents(request={"database": BASE, "documents": [name]}))
                        return next((response.found for response in responses if response.found.name), None)
                    for change in changes:
                        small_policy.full_document(change, read_existing)
                        document_names.append(Write(change).update.name)
                writes = len(changes)
            if self.small:
                self.ledger.reserve(reads=reads, writes=writes, rpc=True, document_names=document_names)
            else:
                self.ledger.reserve(reads=reads, writes=writes)
            self.ledger.record(method)
            if method == "commit" and self.before_commit is not None:
                self.before_commit()
            kwargs.update(retry=None, timeout=10 if method == "commit" else 55)
            from google.api_core.exceptions import FailedPrecondition
            try:
                result = getattr(self.api, method)(request=request, **kwargs)
            except FailedPrecondition as exc:
                if not hasattr(exc, "capacity_failure_origin"):
                    exc.capacity_failure_origin = "rpc_" + method
                raise
            if method == "commit":
                return result
            def stream():
                count = 0
                try:
                    for response in result:
                        present = bool(response.document.name) if method == "run_query" else bool(response.found.name)
                        count += int(present)
                        if method == "run_query" and count > (100 if self.small else 100_000):
                            raise Refused("100文書超過。部分集計を破棄" if self.small
                                          else "10万文書超過。部分集計を破棄")
                        yield response
                except FailedPrecondition as exc:
                    if not hasattr(exc, "capacity_failure_origin"):
                        exc.capacity_failure_origin = "rpc_" + method
                    raise
                finally:
                    # 強制killでは最後の応答数は欠測。事前予約は残るので上限は減らない。
                    self.ledger.transact(lambda data: data.update(
                        returned_documents=data["returned_documents"]+count))
            return stream()
        return call


def make_store(scope, token, ledger, *, role="runner"):
    validate_environment()
    validate_target(PROJECT, DATABASE, scope, role=role)
    ledger.authorize_scope(scope)
    if not token or any(c.isspace() for c in token):
        raise Refused("短命SA tokenが未設定または不正")
    from google.cloud import firestore
    from google.oauth2.credentials import Credentials
    from src.sync_capacity.firestore_store import FirestoreJobStore, Settings
    client = firestore.Client(project=PROJECT, database=DATABASE,
                              credentials=Credentials(token=token),
                              client_options={"api_endpoint": "firestore.googleapis.com"})
    raw = client._firestore_api
    client._firestore_api_internal = GuardedAPI(raw, ledger, role=role)
    return FirestoreJobStore(Settings(PROJECT, DATABASE, scope, 3), client=client)
