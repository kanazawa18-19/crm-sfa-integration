"""推定上限が成立する小規模Firestore要求だけを許可する。"""
from .guard import BASE, Refused

SMALL_SCOPES = {"trial-smoke", "trial-permission"} | {f"trial-concurrency-{n}" for n in range(1, 7)} | {
    f"trial-loss-{name}" for name in ("initialize", "enqueue", "claim", "finish", "recover")}


def resource(name):
    prefix = BASE + "/documents/sync_capacity_scopes/"
    if not name.startswith(prefix) or name[len(prefix):].split("/")[0] not in SMALL_SCOPES:
        raise Refused("小規模試験以外の資源")


def query_shape(query):
    from google.cloud.firestore_v1.types import StructuredQuery
    # claimの固定検索、全件観測、既存確認だけ。cursor・集約等は一致しない。
    variants = [StructuredQuery(from_=[{"collection_id": "jobs"}]),
        StructuredQuery(from_=[{"collection_id": "jobs"}], select={"fields": [
            {"field_path": "state"}, {"field_path": "created_at"}]}),
        StructuredQuery(from_=[{"collection_id": "jobs"}], where={"field_filter": {
            "field": {"field_path": "state"}, "op": "IN",
            "value": {"array_value": {"values": [{"string_value": "pending"}, {"string_value": "retry"}]}}}},
            order_by=[{"field": {"field_path": "available_at"}, "direction": "ASCENDING"}])]
    limit = int(query.limit or 0)
    if limit not in {0, 1, 2, 13, 20, 101}:
        raise Refused("小規模queryのlimitが不正")
    shape = StructuredQuery(query)
    shape.limit = None
    if StructuredQuery.serialize(shape) not in {StructuredQuery.serialize(value) for value in variants}:
        raise Refused("未承認の小規模query条件")
    query.limit = limit or 101
    return int(query.limit or 0)


def full_document(write, read_existing):
    from google.cloud.firestore_v1.types import Document
    from google.cloud.firestore_v1.types import Write
    write = Write(write)
    resource(write.update.name)
    if write.update_transforms:
        raise Refused("小規模試験ではtransformを許可しません")
    if write.update_mask.field_paths:
        # 部分更新後の全体を検査する。比較版が変わっていたら書かずに停止する。
        if not write.current_document.update_time:
            raise Refused("部分更新には版の前提が必要")
        existing = read_existing(write.update.name)
        if existing is None or existing.update_time != write.current_document.update_time:
            from google.api_core.exceptions import FailedPrecondition
            error = FailedPrecondition("小規模試験の更新前版が変わりました")
            error.capacity_failure_origin = "guard_version_check"
            raise error
        result = Document(existing)
        result.create_time = None
        result.update_time = None
        for key in write.update_mask.field_paths:
            if "." in key or key not in write.update.fields:
                raise Refused("未承認の部分更新フィールド")
            result.fields[key] = write.update.fields[key]
    else:
        if write.current_document.exists is not False or not Write.pb(write).current_document.HasField("exists"):
            raise Refused("全体書込みは新規createだけを許可")
        result = Document(write.update)
    allowed = ({"version", "limit", "slots"} if "/jobs/" not in result.name else
        {"source", "event", "payload_hash", "state", "created_at", "available_at", "updated_at",
         "owner", "slot", "attempts", "reason"})
    if set(result.fields) - allowed or len(Document.serialize(result)) > 4096:
        raise Refused("文書の許可フィールドまたは4KiB上限を超過")
    return result
