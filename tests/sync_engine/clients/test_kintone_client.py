"""HttpKintoneClientの単体テスト（実HTTP通信はrequests_mockでモック）。"""

from __future__ import annotations

import pytest

from src.sync_engine.clients.kintone_client import (
    HttpKintoneClient,
    KintoneApiError,
    unwrap_kintone_record,
    wrap_kintone_record,
)

DOMAIN = "example.cybozu.com"
RECORD_URL = f"https://{DOMAIN}/k/v1/record.json"


@pytest.fixture
def client() -> HttpKintoneClient:
    return HttpKintoneClient(DOMAIN, api_token="secret-kintone-token")


# --- 認証情報未設定時のエラー -------------------------------------------------------------------


def test_raises_value_error_when_domain_not_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KINTONE_DOMAIN", raising=False)

    with pytest.raises(ValueError, match="KINTONE_DOMAIN"):
        HttpKintoneClient(api_token="secret-kintone-token")


def test_raises_value_error_when_api_token_not_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KINTONE_API_TOKEN", raising=False)

    with pytest.raises(ValueError, match="KINTONE_API_TOKEN"):
        HttpKintoneClient(DOMAIN)


# --- get_record ------------------------------------------------------------------------


def test_get_record_returns_unwrapped_flat_dict(requests_mock, client: HttpKintoneClient) -> None:
    requests_mock.get(
        RECORD_URL,
        json={"record": {"取引先名": {"value": "テスト商店"}, "TEL": {"value": "03-0000-0000"}}},
    )

    record = client.get_record("1", "1001")

    assert record == {"取引先名": "テスト商店", "TEL": "03-0000-0000"}
    assert requests_mock.last_request.qs["app"] == ["1"]
    assert requests_mock.last_request.qs["id"] == ["1001"]


def test_get_record_returns_none_on_404(requests_mock, client: HttpKintoneClient) -> None:
    requests_mock.get(RECORD_URL, status_code=404)

    assert client.get_record("1", "9999") is None


def test_get_record_sends_api_token_header(requests_mock, client: HttpKintoneClient) -> None:
    requests_mock.get(RECORD_URL, json={"record": {}})

    client.get_record("1", "1001")

    assert requests_mock.last_request.headers["X-Cybozu-API-Token"] == "secret-kintone-token"


def test_get_record_raises_kintone_api_error_on_5xx(
    requests_mock, client: HttpKintoneClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("src.sync_engine.clients._http.time.sleep", lambda seconds: None)
    requests_mock.get(RECORD_URL, status_code=500, json={"message": "internal error"})

    with pytest.raises(KintoneApiError) as exc_info:
        client.get_record("1", "1001")
    assert exc_info.value.status_code == 500


def test_get_record_raises_kintone_api_error_on_200_missing_record_key(
    requests_mock, client: HttpKintoneClient
) -> None:
    """shirokuma-secレビューWARN対応（2026-08-27）: HTTP 200だがボディに`record`キーを欠く
    異常応答の場合、raise_for_error()は2xxを素通りするため、生のKeyErrorではなく正規化された
    KintoneApiErrorになることを確認する。"""
    requests_mock.get(RECORD_URL, status_code=200, json={"unexpected": "shape"})

    with pytest.raises(KintoneApiError) as exc_info:
        client.get_record("1", "1001")
    assert exc_info.value.status_code == 200


# --- add_record --------------------------------------------------------------------------


def test_add_record_sends_wrapped_body_and_returns_id(
    requests_mock, client: HttpKintoneClient
) -> None:
    requests_mock.post(RECORD_URL, json={"id": "1002", "revision": "1"})

    record_id = client.add_record("1", {"取引先名": "新規取引先"})

    assert record_id == "1002"
    sent_body = requests_mock.last_request.json()
    assert sent_body == {"app": "1", "record": {"取引先名": {"value": "新規取引先"}}}


def test_add_record_raises_kintone_api_error_on_400(
    requests_mock, client: HttpKintoneClient
) -> None:
    requests_mock.post(RECORD_URL, status_code=400, json={"message": "invalid"})

    with pytest.raises(KintoneApiError):
        client.add_record("1", {"取引先名": "新規取引先"})


def test_add_record_raises_kintone_api_error_on_200_missing_id_key(
    requests_mock, client: HttpKintoneClient
) -> None:
    """shirokuma-secレビューWARN対応（2026-08-27）: 200応答で`id`キー自体を欠く想定外の
    ボディ形状でも、生のKeyErrorではなくKintoneApiErrorへ正規化されること。"""
    requests_mock.post(RECORD_URL, status_code=200, json={"unexpected": "shape"})

    with pytest.raises(KintoneApiError):
        client.add_record("1", {"取引先名": "新規取引先"})


def test_add_record_does_not_retry_on_5xx(
    requests_mock, client: HttpKintoneClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WARN対応: 作成系（非冪等）操作は5xxでもリトライせず即座にエラーとして返す
    （重複レコード作成を避ける）。
    """
    monkeypatch.setattr("src.sync_engine.clients._http.time.sleep", lambda seconds: None)
    requests_mock.post(RECORD_URL, status_code=500, json={"message": "internal error"})

    with pytest.raises(KintoneApiError):
        client.add_record("1", {"取引先名": "新規取引先"})

    assert requests_mock.call_count == 1


# --- update_record -------------------------------------------------------------------------


def test_update_record_sends_wrapped_body_with_id(
    requests_mock, client: HttpKintoneClient
) -> None:
    requests_mock.put(RECORD_URL, json={"revision": "2"})

    client.update_record("1", "1001", {"取引先名": "更新後"})

    sent_body = requests_mock.last_request.json()
    assert sent_body == {"app": "1", "id": "1001", "record": {"取引先名": {"value": "更新後"}}}


# --- タイムアウト・リトライ ------------------------------------------------------------------


def test_get_record_retries_on_503_then_succeeds(
    requests_mock, client: HttpKintoneClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("src.sync_engine.clients._http.time.sleep", lambda seconds: None)
    requests_mock.get(
        RECORD_URL,
        [
            {"status_code": 503},
            {"json": {"record": {"取引先名": {"value": "テスト商店"}}}, "status_code": 200},
        ],
    )

    record = client.get_record("1", "1001")

    assert record == {"取引先名": "テスト商店"}
    assert requests_mock.call_count == 2


# --- wrap/unwrap変換ロジック -----------------------------------------------------------------


def test_unwrap_kintone_record() -> None:
    assert unwrap_kintone_record({"取引先名": {"value": "テスト商店"}, "TEL": {"value": None}}) == {
        "取引先名": "テスト商店",
        "TEL": None,
    }


def test_wrap_kintone_record() -> None:
    assert wrap_kintone_record({"取引先名": "テスト商店", "削除フラグ": True}) == {
        "取引先名": {"value": "テスト商店"},
        "削除フラグ": {"value": True},
    }


# --- Content-Type の付け方（CB_IL02対策、2026-08-28） ----------------------------------


def test_get_record_does_not_send_content_type_header(
    requests_mock, client: HttpKintoneClient
) -> None:
    """クエリ文字列でパラメータを渡すGETに`Content-Type`を付けないこと。

    kintoneは「ボディが無いのにJSONボディがあると宣言している」不正なリクエストとみなし
    `HTTP 400 (code=CB_IL02) 不正なリクエストです。`を返す。この誤りにより本番の
    `get_record()`が常に失敗していた（2026-08-28、Round2の新規レコード作成が
    external_id 62168〜62171と連番で全件失敗）。
    """
    requests_mock.get(RECORD_URL, json={"record": {}})

    client.get_record("15", "62168")

    assert "Content-Type" not in requests_mock.last_request.headers


def test_add_record_sends_content_type_header(requests_mock, client: HttpKintoneClient) -> None:
    """JSONボディを送る書き込み系では`Content-Type`を付けること（GETと対の保証）。"""
    requests_mock.post(RECORD_URL, json={"id": "1001"})

    client.add_record("15", {"会社名": "テスト"})

    assert requests_mock.last_request.headers["Content-Type"] == "application/json"


def test_update_record_sends_content_type_header(requests_mock, client: HttpKintoneClient) -> None:
    """更新系(PUT)でも`Content-Type`を付けること。"""
    requests_mock.put(RECORD_URL, json={})

    client.update_record("15", "62168", {"会社名": "テスト"})

    assert requests_mock.last_request.headers["Content-Type"] == "application/json"
