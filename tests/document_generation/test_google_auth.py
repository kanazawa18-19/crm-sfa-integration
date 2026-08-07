"""google_auth.get_google_access_token()の単体テスト。

実際のRSA鍵での署名・Google認可サーバーへの通信は行わず、
`service_account.Credentials.from_service_account_info`/`.refresh()`をフェイクに差し替えて
（サービスアカウント優先・有効期限が近い場合の再取得・GOOGLE_ACCESS_TOKENへのフォールバック・
どちらも未設定時のエラー）呼び出し側のロジックのみを検証する。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.document_generation import google_auth


def _naive_utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class _FakeCredentials:
    def __init__(self, *, token: str = "sa-token", expiry: datetime | None = None) -> None:
        self.token = token
        self.expiry = expiry
        self.valid = expiry is not None
        self.refresh_calls = 0

    def refresh(self, request: object) -> None:
        self.refresh_calls += 1
        self.valid = True
        self.token = f"{self.token}-refreshed"
        self.expiry = _naive_utcnow() + timedelta(hours=1)


@pytest.fixture(autouse=True)
def _reset_auth_state(monkeypatch: pytest.MonkeyPatch) -> None:
    google_auth.reset_cache()
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_JSON", raising=False)
    monkeypatch.delenv("GOOGLE_ACCESS_TOKEN", raising=False)
    yield
    google_auth.reset_cache()


def test_raises_when_neither_credential_configured() -> None:
    with pytest.raises(ValueError, match="GOOGLE_ACCESS_TOKEN"):
        google_auth.get_google_access_token()


def test_falls_back_to_manual_token_when_service_account_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOOGLE_ACCESS_TOKEN", "manual-token")

    assert google_auth.get_google_access_token() == "manual-token"


def test_uses_service_account_and_refreshes_when_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", '{"type": "service_account"}')
    fake_credentials = _FakeCredentials(expiry=None)  # 未取得状態（invalid）を模す
    monkeypatch.setattr(
        google_auth.service_account.Credentials,
        "from_service_account_info",
        lambda info, scopes: fake_credentials,
    )

    token = google_auth.get_google_access_token()

    assert token == "sa-token-refreshed"
    assert fake_credentials.refresh_calls == 1


def test_service_account_credentials_are_cached_across_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", '{"type": "service_account"}')
    fake_credentials = _FakeCredentials(expiry=None)
    call_count = 0

    def _from_info(info: dict, scopes: list[str]) -> _FakeCredentials:
        nonlocal call_count
        call_count += 1
        return fake_credentials

    monkeypatch.setattr(google_auth.service_account.Credentials, "from_service_account_info", _from_info)

    google_auth.get_google_access_token()
    google_auth.get_google_access_token()

    assert call_count == 1


def test_refreshes_again_when_token_expires_soon(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", '{"type": "service_account"}')
    # 有効期限まで残り10秒（60秒マージンを下回る）の状態を模す。
    fake_credentials = _FakeCredentials(token="sa-token", expiry=_naive_utcnow() + timedelta(seconds=10))
    fake_credentials.valid = True
    monkeypatch.setattr(
        google_auth.service_account.Credentials,
        "from_service_account_info",
        lambda info, scopes: fake_credentials,
    )

    token = google_auth.get_google_access_token()

    assert fake_credentials.refresh_calls == 1
    assert token == "sa-token-refreshed"


def test_does_not_refresh_when_token_still_valid_for_a_while(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", '{"type": "service_account"}')
    fake_credentials = _FakeCredentials(
        token="still-valid-token", expiry=_naive_utcnow() + timedelta(minutes=30)
    )
    fake_credentials.valid = True
    monkeypatch.setattr(
        google_auth.service_account.Credentials,
        "from_service_account_info",
        lambda info, scopes: fake_credentials,
    )

    token = google_auth.get_google_access_token()

    assert fake_credentials.refresh_calls == 0
    assert token == "still-valid-token"


def test_service_account_takes_priority_over_manual_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_ACCESS_TOKEN", "manual-token")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", '{"type": "service_account"}')
    fake_credentials = _FakeCredentials(
        token="sa-token", expiry=_naive_utcnow() + timedelta(minutes=30)
    )
    fake_credentials.valid = True
    monkeypatch.setattr(
        google_auth.service_account.Credentials,
        "from_service_account_info",
        lambda info, scopes: fake_credentials,
    )

    assert google_auth.get_google_access_token() == "sa-token"
