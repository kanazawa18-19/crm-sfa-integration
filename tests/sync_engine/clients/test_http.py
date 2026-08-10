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


# --- 2026-08-10: 429は非冪等操作でも常にリトライする（Notion本番一括投入対応） -----------


def test_request_with_retry_retries_on_429_even_when_not_idempotent(
    requests_mock, no_sleep
) -> None:
    """BLOCKER相当の修正確認: 作成系（非冪等）操作でも429だけは常にリトライする。
    修正前は非冪等操作の429が即座にraise_for_error()相当のエラーレスポンスとして
    返り、数時間規模の一括作成処理が最初のレート制限到達で丸ごと停止していた。"""
    requests_mock.post(
        "https://example.test/create",
        [
            {"status_code": 429, "json": {"error": "rate limited"}},
            {"status_code": 200, "json": {"id": "created-1"}},
        ],
    )

    response = request_with_retry(
        "POST",
        "https://example.test/create",
        timeout=5,
        max_retries=3,
        sleep=no_sleep.append,
        idempotent=False,
    )

    assert response.status_code == 200
    assert requests_mock.call_count == 2


def test_request_with_retry_honors_retry_after_header_on_429(requests_mock, no_sleep) -> None:
    """Retry-Afterヘッダーがあれば指数バックオフより優先する（サーバー側の実際の
    レート制限ウィンドウ残り時間に即した最短の待機で済ませるため）。"""
    requests_mock.get(
        "https://example.test/rate-limited",
        [
            {"status_code": 429, "headers": {"Retry-After": "2.5"}, "json": {"error": "boom"}},
            {"status_code": 200, "json": {"ok": True}},
        ],
    )

    response = request_with_retry(
        "GET", "https://example.test/rate-limited", timeout=5, sleep=no_sleep.append
    )

    assert response.status_code == 200
    assert no_sleep == [2.5]


def test_request_with_retry_falls_back_to_capped_backoff_when_no_retry_after_header(
    requests_mock, no_sleep
) -> None:
    requests_mock.get(
        "https://example.test/rate-limited-no-header",
        [
            {"status_code": 429, "json": {"error": "boom"}},
            {"status_code": 200, "json": {"ok": True}},
        ],
    )

    response = request_with_retry(
        "GET",
        "https://example.test/rate-limited-no-header",
        timeout=5,
        backoff_base=0.5,
        sleep=no_sleep.append,
    )

    assert response.status_code == 200
    assert no_sleep == [0.5]  # backoff_base * 2^0


def test_request_with_retry_falls_back_when_retry_after_header_is_non_finite(
    requests_mock, no_sleep
) -> None:
    """kuma-qaレビューWARN対応: Retry-Afterが"inf"のような、float()がValueErrorを
    送出しない非有限値だった場合、無条件に信頼すると理論上無限に待機し得た。
    非有限値はヘッダー無し扱いと同様に指数バックオフ（それ自体は
    _MAX_RATE_LIMIT_BACKOFF_SECONDSで頭打ち）へフォールバックすることを確認する。"""
    requests_mock.get(
        "https://example.test/rate-limited-infinite-retry-after",
        [
            {"status_code": 429, "headers": {"Retry-After": "inf"}, "json": {"error": "boom"}},
            {"status_code": 200, "json": {"ok": True}},
        ],
    )

    response = request_with_retry(
        "GET",
        "https://example.test/rate-limited-infinite-retry-after",
        timeout=5,
        backoff_base=0.5,
        sleep=no_sleep.append,
    )

    assert response.status_code == 200
    assert no_sleep == [0.5]  # backoff_base * 2^0（"inf"はそのまま使われない）


def test_request_with_retry_caps_huge_retry_after_header(requests_mock, no_sleep) -> None:
    """異常に大きい（が有限な）Retry-Ather値も同様に上限で頭打ちにする。"""
    requests_mock.get(
        "https://example.test/rate-limited-huge-retry-after",
        [
            {"status_code": 429, "headers": {"Retry-After": "99999999"}, "json": {"error": "boom"}},
            {"status_code": 200, "json": {"ok": True}},
        ],
    )

    response = request_with_retry(
        "GET",
        "https://example.test/rate-limited-huge-retry-after",
        timeout=5,
        sleep=no_sleep.append,
    )

    assert response.status_code == 200
    assert no_sleep == [30.0]  # _MAX_RATE_LIMIT_BACKOFF_SECONDS


def test_request_with_retry_gives_up_after_max_rate_limit_retries(
    requests_mock, no_sleep
) -> None:
    """429が解消しない場合でも無限リトライにはせず、上限到達で最後の429応答を返す
    （呼び出し元がraise_for_error()等で最終的にエラー扱いできるようにする安全弁）。"""
    requests_mock.get("https://example.test/always-rate-limited", status_code=429, json={})

    response = request_with_retry(
        "GET",
        "https://example.test/always-rate-limited",
        timeout=5,
        max_rate_limit_retries=2,
        sleep=no_sleep.append,
    )

    assert response.status_code == 429
    assert requests_mock.call_count == 3  # 初回 + リトライ2回
    assert len(no_sleep) == 2


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
