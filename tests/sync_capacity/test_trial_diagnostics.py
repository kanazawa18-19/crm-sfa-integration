"""混在ログから固定診断だけを取り出し、欠測を成功にしない。"""
import json

import pytest

from scripts.capacity_trial.diagnostics import PREFIX, failure_record, parse_failure


def encoded(**overrides):
    value = {"state": "failed", "partial_result": False,
             "error_type": "FailedPrecondition", "error_code": "unexpected_error"}
    value.update(overrides)
    return PREFIX + json.dumps(value)


def test_sdk_noise_does_not_erase_classification_or_leak_body():
    result = parse_failure("SDK synthetic-secret\n" + encoded() + "\nSDK trailing-secret\n")
    assert result == {"error_type": "FailedPrecondition", "error_code": "unexpected_error",
                      "diagnostic_state": "classified"}
    assert "secret" not in json.dumps(result)


@pytest.mark.parametrize("output,expected", [
    ("SDK synthetic-secret", "missing"),
    (encoded() + "\n" + encoded(), "ambiguous"),
    (PREFIX + "broken-synthetic-secret", "invalid"),
    (encoded(state="passed"), "invalid"),
    (encoded(partial_result=True), "invalid"),
    (encoded(secret="synthetic-secret"), "invalid"),
    (encoded(error_type="synthetic-secret"), "unsupported"),
    (encoded(error_code="synthetic-secret"), "unsupported"),
    (encoded(error_type=[]), "unsupported"),
])
def test_missing_or_untrusted_diagnostics_are_distinct(output, expected):
    result = parse_failure(output)
    assert result["diagnostic_state"] == expected
    assert result["error_type"] == "unknown"
    assert "synthetic-secret" not in json.dumps(result)


def test_exception_message_and_unknown_class_name_are_not_emitted():
    error_class = type("synthetic_secret_class", (Exception,), {})
    result = failure_record(error_class("synthetic-secret"))
    assert result["error_type"] == "unknown" and result["state"] == "failed"
    assert "synthetic" not in json.dumps(result)
    assert parse_failure(PREFIX + json.dumps(failure_record(ValueError("synthetic-secret"))))[
        "error_type"] == "ValueError"


@pytest.mark.parametrize("internal", [False, True])
def test_real_cli_failure_after_unterminated_sdk_log(internal):
    """実CLIの例外出口を通し、改行なしログと本文非公開を確認する。"""
    import os
    import subprocess
    import sys
    from pathlib import Path

    env = dict(os.environ)
    env.pop("CAPACITY_TRIAL_CHILD", None)
    if internal:
        env["CAPACITY_TRIAL_CHILD"] = "1"
    code = """
import argparse, runpy, sys
def fail(*args, **kwargs):
    sys.stderr.write('SDK synthetic-secret' if INTERNAL else '')
    raise ValueError('synthetic-secret')
argparse.ArgumentParser.parse_args = fail
runpy.run_module('scripts.capacity_trial', run_name='__main__')
""".replace("INTERNAL", repr(internal))
    result = subprocess.run([sys.executable, "-s", "-c", code], env=env,
                            cwd=Path(__file__).resolve().parents[2],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 1 and result.stdout == ""
    if internal:
        assert parse_failure(result.stderr) == {
            "error_type": "ValueError", "error_code": "unexpected_error",
            "diagnostic_state": "classified"}
    else:
        assert json.loads(result.stderr) == failure_record(ValueError())
        assert "synthetic-secret" not in result.stderr


def atomic_record(**changes):
    value = dict(attempts=8, elapsed_ms=200, phase="commit", non_conflict_chain=False,
                 attempt_limit=True, deadline=False)
    value.update(changes)
    return value


def test_optional_diagnostics_roundtrip_without_exception_body():
    from google.api_core.exceptions import FailedPrecondition
    error = FailedPrecondition("synthetic-secret")
    error.capacity_failure_origin = "rpc_commit"
    error.capacity_atomic = atomic_record()
    result = parse_failure(PREFIX + json.dumps(failure_record(error)))
    assert result["origin"] == "rpc_commit" and result["atomic"] == atomic_record()
    assert "synthetic-secret" not in json.dumps(result)


@pytest.mark.parametrize("fields", [
    {"origin": "synthetic-secret"}, {"origin": []},
    {"atomic": atomic_record(attempts=True)}, {"atomic": atomic_record(attempts=9)},
    {"atomic": atomic_record(elapsed_ms=-1)}, {"atomic": atomic_record(elapsed_ms=86400001)},
    {"atomic": atomic_record(phase="synthetic-secret")},
    {"atomic": atomic_record(attempt_limit=False)},
    {"atomic": atomic_record(deadline=True)}, {"atomic": atomic_record(secret="synthetic-secret")},
])
def test_invalid_optional_diagnostics_never_classify(fields):
    result = parse_failure(encoded(**fields))
    assert result["diagnostic_state"] == "invalid" and result["error_type"] == "unknown"
    assert "synthetic-secret" not in json.dumps(result)



def test_invalid_internal_atomic_metadata_is_not_silently_dropped():
    error = ValueError("synthetic-secret")
    error.capacity_atomic = {"secret": "synthetic-secret"}
    record = failure_record(error)
    assert "synthetic-secret" not in json.dumps(record)
    assert parse_failure(PREFIX + json.dumps(record))["diagnostic_state"] == "invalid"


@pytest.mark.parametrize("name", ["FailedPrecondition", "Aborted", "AlreadyExists"])
def test_all_comparison_conflicts_without_marker_have_unknown_origin(name):
    from google.api_core import exceptions
    record = failure_record(getattr(exceptions, name)("synthetic-secret"))
    assert record["origin"] == "unknown"
    assert "synthetic-secret" not in json.dumps(record)
