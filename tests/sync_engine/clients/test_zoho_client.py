"""HttpZohoClientの単体テスト（実HTTP通信はrequests_mockでモック）。"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from src.sync_engine.clients.zoho_client import HttpZohoClient, ZohoApiError

TOKEN_URL = "https://accounts.zoho.com/oauth/v2/token"
RECORD_URL = "https://www.zohoapis.com/crm/v2/Deals/12345"
MODULE_URL = "https://www.zohoapis.com/crm/v2/Deals"


@pytest.fixture
def client() -> HttpZohoClient:
    return HttpZohoClient(
        client_id="cid", client_secret="csecret", refresh_token="rtoken"
    )


# --- 認証情報未設定時のエラー -------------------------------------------------------------------


def test_raises_value_error_when_client_id_not_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ZOHO_CLIENT_ID", raising=False)

    with pytest.raises(ValueError, match="ZOHO_CLIENT_ID"):
        HttpZohoClient(client_secret="csecret", refresh_token="rtoken")


def test_raises_value_error_when_client_secret_not_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ZOHO_CLIENT_SECRET", raising=False)

    with pytest.raises(ValueError, match="ZOHO_CLIENT_SECRET"):
        HttpZohoClient(client_id="cid", refresh_token="rtoken")


def test_raises_value_error_when_refresh_token_not_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ZOHO_REFRESH_TOKEN", raising=False)

    with pytest.raises(ValueError, match="ZOHO_REFRESH_TOKEN"):
        HttpZohoClient(client_id="cid", client_secret="csecret")


def _mock_token(requests_mock, *, access_token: str = "access-token-1", expires_in: int = 3600) -> None:
    requests_mock.post(
        TOKEN_URL, json={"access_token": access_token, "expires_in": expires_in, "api_domain": "https://www.zohoapis.com"}
    )


# --- アクセストークン取得・キャッシュ ---------------------------------------------------------


def test_get_record_fetches_access_token_and_uses_it(requests_mock, client: HttpZohoClient) -> None:
    _mock_token(requests_mock)
    requests_mock.get(RECORD_URL, json={"data": [{"id": "12345", "Deal_Name": "サンプル案件"}]})

    record = client.get_record("Deals", "12345")

    assert record == {"id": "12345", "Deal_Name": "サンプル案件"}
    token_request = requests_mock.request_history[0]
    assert token_request.url.startswith(TOKEN_URL)
    assert requests_mock.last_request.headers["Authorization"] == "Zoho-oauthtoken access-token-1"


def test_access_token_is_cached_across_calls(requests_mock, client: HttpZohoClient) -> None:
    _mock_token(requests_mock)
    requests_mock.get(RECORD_URL, json={"data": [{"id": "12345"}]})

    client.get_record("Deals", "12345")
    client.get_record("Deals", "12345")

    token_calls = [req for req in requests_mock.request_history if req.url.startswith(TOKEN_URL)]
    assert len(token_calls) == 1  # 2回目はキャッシュされたトークンを再利用


def test_access_token_is_refreshed_once_expired(requests_mock, client: HttpZohoClient) -> None:
    _mock_token(requests_mock)
    requests_mock.get(RECORD_URL, json={"data": [{"id": "12345"}]})

    client.get_record("Deals", "12345")
    # トークンをテスト用に強制的に失効させる（実際の経過待ちを回避するため直接操作する）。
    client._access_token_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    client.get_record("Deals", "12345")

    token_calls = [req for req in requests_mock.request_history if req.url.startswith(TOKEN_URL)]
    assert len(token_calls) == 2


def test_concurrent_access_token_refresh_only_refreshes_once(
    requests_mock, client: HttpZohoClient
) -> None:
    """WARN対応: 複数スレッドが同時に期限切れと判定しても、threading.Lockにより
    実際のリフレッシュ（トークンエンドポイントへのPOST）は1回しか発火しない。
    """
    call_count = {"n": 0}
    count_lock = threading.Lock()

    def token_callback(request, context):
        with count_lock:
            call_count["n"] += 1
        time.sleep(0.05)  # 複数スレッドのリクエストが重なる時間を確保する
        context.status_code = 200
        return {"access_token": "concurrent-token", "expires_in": 3600}

    requests_mock.post(TOKEN_URL, json=token_callback)

    results: list[str] = []
    results_lock = threading.Lock()

    def worker() -> None:
        token = client._get_access_token()
        with results_lock:
            results.append(token)

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert call_count["n"] == 1
    assert results == ["concurrent-token"] * 5


def test_token_refresh_raises_zoho_api_error_on_failure(
    requests_mock, client: HttpZohoClient
) -> None:
    requests_mock.post(TOKEN_URL, status_code=400, json={"error": "invalid_code"})

    with pytest.raises(ZohoApiError):
        client.get_record("Deals", "12345")


def test_token_refresh_raises_zoho_api_error_on_200_with_error_body(
    requests_mock, client: HttpZohoClient
) -> None:
    """shirokuma-secレビューWARN対応（2026-08-27）: ZohoのOAuthトークンエンドポイントは
    リフレッシュトークン失効・クライアント資格情報不正の際、HTTP 200のままエラーボディを
    返すことが知られている。raise_for_error()は2xxを素通りするため、body["access_token"]の
    生のKeyErrorではなく正規化されたZohoApiErrorになることを確認する。"""
    requests_mock.post(TOKEN_URL, status_code=200, json={"error": "invalid_client"})

    with pytest.raises(ZohoApiError) as exc_info:
        client.get_record("Deals", "12345")
    assert exc_info.value.status_code == 200


def test_token_refresh_raises_zoho_api_error_on_200_missing_access_token_key(
    requests_mock, client: HttpZohoClient
) -> None:
    """access_tokenキー自体を欠く200応答（想定外のボディ形状）でも同様にZohoApiErrorへ
    正規化されること。"""
    requests_mock.post(TOKEN_URL, status_code=200, json={"unexpected": "shape"})

    with pytest.raises(ZohoApiError):
        client.get_record("Deals", "12345")


# --- get_record ------------------------------------------------------------------------


def test_get_record_returns_none_on_404(requests_mock, client: HttpZohoClient) -> None:
    _mock_token(requests_mock)
    requests_mock.get(RECORD_URL, status_code=404)

    assert client.get_record("Deals", "12345") is None


def test_get_record_returns_none_when_data_empty(requests_mock, client: HttpZohoClient) -> None:
    _mock_token(requests_mock)
    requests_mock.get(RECORD_URL, json={"data": []})

    assert client.get_record("Deals", "12345") is None


def test_get_record_raises_zoho_api_error_on_5xx(
    requests_mock, client: HttpZohoClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("src.sync_engine.clients._http.time.sleep", lambda seconds: None)
    _mock_token(requests_mock)
    requests_mock.get(RECORD_URL, status_code=500, json={"message": "internal error"})

    with pytest.raises(ZohoApiError):
        client.get_record("Deals", "12345")


def test_get_record_raises_zoho_api_error_on_200_when_data_is_not_a_list(
    requests_mock, client: HttpZohoClient
) -> None:
    """shirokuma-secレビューBLOCKER対応（2026-08-28）: HTTP 200だが`data`が想定したリスト
    形式ではない異常応答の場合、生のKeyErrorではなく正規化されたZohoApiErrorになることを
    確認する（`insert_record`/`update_record`と同じ穴が`get_record`にも残っていた）。"""
    _mock_token(requests_mock)
    requests_mock.get(RECORD_URL, status_code=200, json={"data": {"unexpected": "shape"}})

    with pytest.raises(ZohoApiError) as exc_info:
        client.get_record("Deals", "12345")
    assert exc_info.value.status_code == 200


def test_get_record_raises_zoho_api_error_on_200_when_body_is_not_a_dict(
    requests_mock, client: HttpZohoClient
) -> None:
    """HTTP 200だがレスポンスボディそのものが辞書ではない異常応答の場合、生のAttributeErrorでは
    なく正規化されたZohoApiErrorになることを確認する。"""
    _mock_token(requests_mock)
    requests_mock.get(RECORD_URL, status_code=200, json=["unexpected", "shape"])

    with pytest.raises(ZohoApiError):
        client.get_record("Deals", "12345")


# --- insert_record -------------------------------------------------------------------------


def test_insert_record_sends_wrapped_body_and_returns_id(
    requests_mock, client: HttpZohoClient
) -> None:
    _mock_token(requests_mock)
    requests_mock.post(
        MODULE_URL,
        json={"data": [{"code": "SUCCESS", "status": "success", "details": {"id": "99999"}}]},
    )

    record_id = client.insert_record("Deals", {"Deal_Name": "新規案件"})

    assert record_id == "99999"
    sent_body = requests_mock.last_request.json()
    assert sent_body == {"data": [{"Deal_Name": "新規案件"}]}


def test_insert_record_raises_zoho_api_error_when_code_not_success(
    requests_mock, client: HttpZohoClient
) -> None:
    _mock_token(requests_mock)
    requests_mock.post(
        MODULE_URL,
        json={"data": [{"code": "DUPLICATE_DATA", "message": "duplicate"}]},
    )

    with pytest.raises(ZohoApiError):
        client.insert_record("Deals", {"Deal_Name": "新規案件"})


def test_insert_record_raises_zoho_api_error_when_data_key_missing(
    requests_mock, client: HttpZohoClient
) -> None:
    """shirokuma-secレビューWARN対応（2026-08-27）: 200応答で`data`キー自体を欠く想定外の
    ボディ形状でも、生のKeyError/IndexErrorではなくZohoApiErrorへ正規化されること。"""
    _mock_token(requests_mock)
    requests_mock.post(MODULE_URL, status_code=200, json={"unexpected": "shape"})

    with pytest.raises(ZohoApiError):
        client.insert_record("Deals", {"Deal_Name": "新規案件"})


def test_insert_record_does_not_retry_on_5xx(
    requests_mock, client: HttpZohoClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WARN対応: 作成系（非冪等）操作は5xxでもリトライせず即座にエラーとして返す
    （重複レコード作成を避ける）。
    """
    monkeypatch.setattr("src.sync_engine.clients._http.time.sleep", lambda seconds: None)
    _mock_token(requests_mock)
    requests_mock.post(MODULE_URL, status_code=500, json={"message": "internal error"})

    with pytest.raises(ZohoApiError):
        client.insert_record("Deals", {"Deal_Name": "新規案件"})

    module_calls = [req for req in requests_mock.request_history if req.url == MODULE_URL]
    assert len(module_calls) == 1


# --- update_record -------------------------------------------------------------------------


def test_update_record_sends_body_with_id(requests_mock, client: HttpZohoClient) -> None:
    _mock_token(requests_mock)
    requests_mock.put(
        RECORD_URL,
        json={"data": [{"code": "SUCCESS", "status": "success", "details": {"id": "12345"}}]},
    )

    client.update_record("Deals", "12345", {"Deal_Name": "更新後案件"})

    sent_body = requests_mock.last_request.json()
    assert sent_body == {"data": [{"Deal_Name": "更新後案件", "id": "12345"}]}


def test_update_record_raises_zoho_api_error_when_code_not_success(
    requests_mock, client: HttpZohoClient
) -> None:
    _mock_token(requests_mock)
    requests_mock.put(
        RECORD_URL,
        json={"data": [{"code": "INVALID_DATA", "message": "invalid"}]},
    )

    with pytest.raises(ZohoApiError):
        client.update_record("Deals", "12345", {"Deal_Name": "更新後案件"})


# --- タイムアウト・リトライ ------------------------------------------------------------------


# --- カスタムaccounts_base_url/api_base_url（データセンター別対応） -----------------------------


def test_custom_base_urls_are_used_for_token_refresh_and_api_calls(requests_mock) -> None:
    """accounts_base_url/api_base_url引数を指定した場合、既定の`.com`ではなく指定した
    ベースURL（例: `.jp`データセンター）へ実際にリクエストが送られること。"""
    jp_client = HttpZohoClient(
        client_id="cid",
        client_secret="csecret",
        refresh_token="rtoken",
        accounts_base_url="https://accounts.zoho.jp",
        api_base_url="https://www.zohoapis.jp/crm/v2",
    )
    requests_mock.post(
        "https://accounts.zoho.jp/oauth/v2/token",
        json={"access_token": "jp-access-token", "expires_in": 3600},
    )
    requests_mock.get(
        "https://www.zohoapis.jp/crm/v2/Deals/12345",
        json={"data": [{"id": "12345", "Deal_Name": "サンプル案件"}]},
    )

    record = jp_client.get_record("Deals", "12345")

    assert record == {"id": "12345", "Deal_Name": "サンプル案件"}
    assert not any(req.url.startswith(TOKEN_URL) for req in requests_mock.request_history)
    assert not any(req.url == RECORD_URL for req in requests_mock.request_history)


def test_get_record_retries_on_429_then_succeeds(
    requests_mock, client: HttpZohoClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("src.sync_engine.clients._http.time.sleep", lambda seconds: None)
    _mock_token(requests_mock)
    requests_mock.get(
        RECORD_URL,
        [
            {"status_code": 429},
            {"json": {"data": [{"id": "12345"}]}, "status_code": 200},
        ],
    )

    record = client.get_record("Deals", "12345")

    assert record == {"id": "12345"}
    record_calls = [req for req in requests_mock.request_history if req.url == RECORD_URL]
    assert len(record_calls) == 2
