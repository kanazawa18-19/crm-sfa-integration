"""SDKのログと区別して、失敗の固定分類だけをプロセス間で渡す。"""
import json

from .guard import ERROR_CODES, Refused

PREFIX = "CAPACITY_TRIAL_FAILURE_V1 "
PREFIX_V2 = "CAPACITY_TRIAL_FAILURE_V2 "
ERROR_TYPES = frozenset({
    "Refused", "FailedPrecondition", "Aborted", "AlreadyExists", "CapacityUnavailable",
    "ServiceUnavailable", "DeadlineExceeded", "PermissionDenied", "AssertionError",
    "ValueError", "TypeError", "KeyError", "RuntimeError", "TimeoutError", "RetryError",
    "ResourceExhausted", "Unauthenticated", "Unauthorized", "InternalServerError",
    "InvalidArgument", "NotFound", "Cancelled", "JSONDecodeError", "BrokenPipeError",
    "OwnerMismatch", "PayloadConflict", "CapacityDeadlineExceeded", "unknown",
})


ORIGINS = frozenset({"guard_version_check", "rpc_commit", "rpc_run_query",
                     "rpc_batch_get_documents", "unknown"})
ATOMIC_KEYS = {"attempts", "elapsed_ms", "phase", "non_conflict_chain", "attempt_limit", "deadline"}


def valid_atomic(value):
    return (isinstance(value, dict) and set(value) == ATOMIC_KEYS
            and type(value["attempts"]) is int and 1 <= value["attempts"] <= 8
            and type(value["elapsed_ms"]) is int and 0 <= value["elapsed_ms"] <= 86_400_000
            and isinstance(value["phase"], str) and value["phase"] in {"operation", "commit"}
            and all(type(value[key]) is bool for key in
                    ("non_conflict_chain", "attempt_limit", "deadline"))
            and any(value[key] for key in ("non_conflict_chain", "attempt_limit", "deadline"))
            and value["attempt_limit"] == (value["attempts"] == 8)
            and value["deadline"] == (value["elapsed_ms"] >= 3000))


def valid_atomic_v2(value):
    return (isinstance(value, dict)
            and set(value) == {"version", "attempts", "elapsed_ms", "phase", "stop_reason"}
            and type(value["version"]) is int and value["version"] == 2
            and type(value["attempts"]) is int and 1 <= value["attempts"] <= 8
            and type(value["elapsed_ms"]) is int and 0 <= value["elapsed_ms"] <= 86_400_000
            and isinstance(value["phase"], str) and value["phase"] in {"operation", "commit"}
            and isinstance(value["stop_reason"], str)
            and value["stop_reason"] in {"total_deadline", "attempt_budget", "attempt_limit", "non_conflict_chain"}
            and (value["stop_reason"] != "attempt_limit" or value["attempts"] == 8))


def failure_record(exc):
    name = type(exc).__name__
    record = {"state": "failed", "error_type": name if name in ERROR_TYPES else "unknown",
            "error_code": exc.code if isinstance(exc, Refused) else "unexpected_error",
            "partial_result": False}
    origin = getattr(exc, "capacity_failure_origin", None)
    if origin is not None or name in {"FailedPrecondition", "Aborted", "AlreadyExists"}:
        record["origin"] = origin if isinstance(origin, str) and origin in ORIGINS else "unknown"
    atomic = getattr(exc, "capacity_atomic", None)
    if atomic is not None:
        # 不正な追加診断を欠測扱いにせず、受信側でinvalidにする。
        record["atomic"] = atomic if valid_atomic(atomic) or valid_atomic_v2(atomic) else None
    return record


def parse_failure(output):
    """非0終了専用。候補の欠測・破損・複数を区別し、任意本文を捨てる。"""
    unknown = {"error_type": "unknown", "error_code": "unexpected_error"}
    candidates = [(prefix, line[len(prefix):]) for line in output.splitlines()
                  for prefix in (PREFIX, PREFIX_V2) if line.startswith(prefix)]
    if not candidates:
        return {**unknown, "diagnostic_state": "missing"}
    if len(candidates) != 1:
        return {**unknown, "diagnostic_state": "ambiguous"}
    try:
        prefix, payload = candidates[0]
        value = json.loads(payload)
    except (ValueError, TypeError):
        return {**unknown, "diagnostic_state": "invalid"}
    if (not isinstance(value, dict)
            or not {"state", "error_type", "error_code", "partial_result"} <= set(value)
            or set(value) - {"state", "error_type", "error_code", "partial_result", "origin", "atomic"}
            or value["state"] != "failed" or value["partial_result"] is not False):
        return {**unknown, "diagnostic_state": "invalid"}
    if (("origin" in value and (not isinstance(value["origin"], str) or value["origin"] not in ORIGINS))
            or ("atomic" in value and not (valid_atomic(value["atomic"]) if prefix == PREFIX
                                              else valid_atomic_v2(value["atomic"])))):
        return {**unknown, "diagnostic_state": "invalid"}
    if (not isinstance(value["error_type"], str) or value["error_type"] not in ERROR_TYPES
            or not isinstance(value["error_code"], str) or value["error_code"] not in ERROR_CODES):
        return {**unknown, "diagnostic_state": "unsupported"}
    return {**{key: value[key] for key in ("origin", "atomic") if key in value},
            "error_type": value["error_type"], "error_code": value["error_code"],
            "diagnostic_state": "classified" if value["error_type"] != "unknown" else "unsupported"}


def serialize_failure(exc, *, child=False):
    """有効な旧診断だけV1で出力し、保存済み証跡との往復互換を保つ。"""
    record = failure_record(exc)
    prefix = PREFIX if valid_atomic(record.get("atomic")) else PREFIX_V2
    return ("\n" + prefix if child else "") + json.dumps(record)
