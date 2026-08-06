"""共有HTTPヘルパー（タイムアウト・簡易リトライ）の単体テスト。"""

from __future__ import annotations

import pytest
import requests

from src.sync_engine.clients._http import ApiError, raise_for_error, request_with_retry


@pytest.fixture
def no_sleep() -> list[float]:
    """テストを実際に待たせないためのsleepの差し替え。呼び出された待機秒数を記録する。"""
    return []


def test_request_with_retry_returns_response_on_immediate_success(requests_mock) -> None:
    requests_mock.get("https://example.test/ok", json={"ok": True}, status_code=200)

    response = request_with_retry("GET", "https://example.test/ok", timeout=5)

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_request_with_retry_does_not_retry_on_4xx(requests_mock, no_sleep) -> None:
    requests_mock.get("https://example.test/bad", status_code=400, json={"error": "bad"})

    response = request_with_retry(
        "GET", "https://example.test/bad", timeout=5, sleep=no_sleep.append
    )

    assert response.status_code == 400
    assert requests_mock.call_count == 1
    assert no_sleep == []


def test_request_with_retry_retries_on_429_then_succeeds(requests_mock, no_sleep) -> None:
    requests_mock.get(
        "https://example.test/rate-limited",
        [
            {"status_code": 429, "json": {"error": "rate limited"}},
            {"status_code": 200, "json": {"ok": True}},
        ],
    )

    response = request_with_retry(
        "GET",
        "https://example.test/rate-limited",
        timeout=5,
        max_retries=3,
        sleep=no_sleep.append,
    )

    assert response.status_code == 200
    assert requests_mock.call_count == 2
    assert no_sleep == [0.5]  # 指数バックオフ: backoff_base * 2^0


def test_request_with_retry_retries_on_5xx_up_to_max_retries_then_returns_last_response(
    requests_mock, no_sleep
) -> None:
    requests_mock.get("https://example.test/always-500", status_code=500, json={"error": "boom"})

    response = request_with_retry(
        "GET",
        "https://example.test/always-500",
        timeout=5,
        max_retries=2,
        sleep=no_sleep.append,
    )

    assert response.status_code == 500
    assert requests_mock.call_count == 3  # 初回 + リトライ2回
    assert no_sleep == [0.5, 1.0]


def test_request_with_retry_retries_on_timeout_then_reraises_after_max_retries(
    requests_mock, no_sleep
) -> None:
    requests_mock.get(
        "https://example.test/timeout", exc=requests.exceptions.Timeout("timed out")
    )

    with pytest.raises(requests.exceptions.Timeout):
        request_with_retry(
            "GET",
            "https://example.test/timeout",
            timeout=5,
            max_retries=2,
            sleep=no_sleep.append,
        )

    assert requests_mock.call_count == 3  # 初回 + リトライ2回
    assert no_sleep == [0.5, 1.0]


def test_request_with_retry_does_not_retry_on_5xx_when_not_idempotent(
    requests_mock, no_sleep
) -> None:
    """WARN対応: 作成系（非冪等）操作はmax_retriesの指定に関わらずリトライしない。"""
    requests_mock.post("https://example.test/create", status_code=500, json={"error": "boom"})

    response = request_with_retry(
        "POST",
        "https://example.test/create",
        timeout=5,
        max_retries=3,
        sleep=no_sleep.append,
        idempotent=False,
    )

    assert response.status_code == 500
    assert requests_mock.call_count == 1
    assert no_sleep == []


def test_request_with_retry_does_not_retry_on_timeout_when_not_idempotent(
    requests_mock, no_sleep
) -> None:
    requests_mock.post(
        "https://example.test/create-timeout", exc=requests.exceptions.Timeout("timed out")
    )

    with pytest.raises(requests.exceptions.Timeout):
        request_with_retry(
            "POST",
            "https://example.test/create-timeout",
            timeout=5,
            max_retries=3,
            sleep=no_sleep.append,
            idempotent=False,
        )

    assert requests_mock.call_count == 1
    assert no_sleep == []


def test_request_with_retry_default_sleep_is_patchable_via_module_attribute(
    requests_mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回帰テスト: デフォルトのsleep（未指定時）がtime.sleepへのモジュール属性パッチで
    差し替え可能であることを確認する（デフォルト引数として関数を直接束縛すると
    モンキーパッチが効かなくなるリグレッションを防ぐ）。
    """
    recorded: list[float] = []
    monkeypatch.setattr(
        "src.sync_engine.clients._http.time.sleep", lambda seconds: recorded.append(seconds)
    )
    requests_mock.get("https://example.test/always-500", status_code=500, json={"error": "boom"})

    response = request_with_retry(
        "GET", "https://example.test/always-500", timeout=5, max_retries=2
    )

    assert response.status_code == 500
    assert recorded == [0.5, 1.0]


def test_raise_for_error_raises_configured_exception_class_with_status_code(requests_mock) -> None:
    requests_mock.get(
        "https://example.test/not-found", status_code=404, json={"message": "not found"}
    )
    response = request_with_retry("GET", "https://example.test/not-found", timeout=5)

    class MyApiError(ApiError):
        pass

    with pytest.raises(MyApiError) as exc_info:
        raise_for_error(response, MyApiError)

    assert exc_info.value.status_code == 404
    assert "not found" in exc_info.value.message


def test_raise_for_error_does_nothing_on_success(requests_mock) -> None:
    requests_mock.get("https://example.test/ok", status_code=200, json={})
    response = request_with_retry("GET", "https://example.test/ok", timeout=5)

    raise_for_error(response, ApiError)  # noqa: no exception expected
