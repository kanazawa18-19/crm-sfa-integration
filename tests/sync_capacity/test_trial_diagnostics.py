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
