"""SDKのログと区別して、失敗の固定分類だけをプロセス間で渡す。"""
import json

from .guard import ERROR_CODES, Refused

PREFIX = "CAPACITY_TRIAL_FAILURE_V1 "
ERROR_TYPES = frozenset({
    "Refused", "FailedPrecondition", "Aborted", "AlreadyExists", "CapacityUnavailable",
    "ServiceUnavailable", "DeadlineExceeded", "PermissionDenied", "AssertionError",
    "ValueError", "TypeError", "KeyError", "RuntimeError", "TimeoutError", "RetryError",
    "ResourceExhausted", "Unauthenticated", "Unauthorized", "InternalServerError",
    "InvalidArgument", "NotFound", "Cancelled", "JSONDecodeError", "BrokenPipeError",
    "OwnerMismatch", "PayloadConflict", "unknown",
})


def failure_record(exc):
    name = type(exc).__name__
    return {"state": "failed", "error_type": name if name in ERROR_TYPES else "unknown",
            "error_code": exc.code if isinstance(exc, Refused) else "unexpected_error",
            "partial_result": False}


def parse_failure(output):
    """非0終了専用。候補の欠測・破損・複数を区別し、任意本文を捨てる。"""
    unknown = {"error_type": "unknown", "error_code": "unexpected_error"}
    candidates = [line[len(PREFIX):] for line in output.splitlines() if line.startswith(PREFIX)]
    if not candidates:
        return {**unknown, "diagnostic_state": "missing"}
    if len(candidates) != 1:
        return {**unknown, "diagnostic_state": "ambiguous"}
    try:
        value = json.loads(candidates[0])
    except (ValueError, TypeError):
        return {**unknown, "diagnostic_state": "invalid"}
    if (not isinstance(value, dict)
            or set(value) != {"state", "error_type", "error_code", "partial_result"}
            or value["state"] != "failed" or value["partial_result"] is not False):
        return {**unknown, "diagnostic_state": "invalid"}
    if (not isinstance(value["error_type"], str) or value["error_type"] not in ERROR_TYPES
            or not isinstance(value["error_code"], str) or value["error_code"] not in ERROR_CODES):
        return {**unknown, "diagnostic_state": "unsupported"}
    return {"error_type": value["error_type"], "error_code": value["error_code"],
            "diagnostic_state": "classified" if value["error_type"] != "unknown" else "unsupported"}
