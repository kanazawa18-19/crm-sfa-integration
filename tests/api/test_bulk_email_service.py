"""一斉配信プレビューのユースケース層の検証（2026-09-03）。

判断そのもの（誰に送れるか・法定表示が揃っているか）は`tests/bulk_email/test_preview.py`
が見ている。ここで見るのは**I/Oと形式変換**だけ ——
Notionの表示用dictを宛先に変換できているか、配信停止の読み取りに渡す値が正しいか、
打ち切りの警告が画面まで届くか。
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

import pytest

from src.api import bulk_email_service
from src.api.bulk_email_service import (
    build_bulk_email_preview,
    build_consent_overview,
    unsubscribe_base_url,
)
from src.api.client_360_service import Client360DataSource
from src.bulk_email.consent import BASIS_NOTIFIED, REASON_MISSING, ConsentRecord
from src.sync_engine.clients.notion_client import NotionApiError


class _FakeQueryClient:
    def __init__(self, pages: list[dict[str, Any]] | None = None) -> None:
        self._pages = pages or []

    def query_page(
        self, *, page_size: int = 100, filter: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        return self._pages


class _FakeClientMasterClient(_FakeQueryClient):
    def __init__(self, raw_pages: dict[str, dict[str, Any]] | None = None) -> None:
        super().__init__()
        self._raw_pages = raw_pages or {}

    def get_raw_page(self, page_id: str) -> dict[str, Any]:
        if page_id not in self._raw_pages:
            raise NotionApiError(404, "not found")
        return self._raw_pages[page_id]


class _FakeUserDirectory:
    def resolve(self, user_id: str) -> str:
        return user_id

    def resolve_many(self, user_ids: list[str]) -> list[str]:
        return list(user_ids)


class _FakePreferenceReader:
    """配信停止と送信根拠のスタブ。実際に渡された候補も記録する。

    `consents`の既定は`None`＝「テストの連絡先には全部根拠がある」。
    根拠そのものを見るテストだけが明示的に渡す（このテストが見たいのは
    I/Oと形式変換で、根拠の判定は`tests/bulk_email/`側の仕事）。
    """

    def __init__(
        self,
        page_ids: set[str] | None = None,
        emails: set[str] | None = None,
        consents: list[ConsentRecord] | None = None,
    ) -> None:
        self.page_ids = page_ids or set()
        self.emails = emails or set()
        self._consents = consents
        self.calls: list[tuple[list[str], list[str]]] = []
        self.consent_calls: list[tuple[list[str], list[str]]] = []

    def fetch_opt_outs(self, page_ids, emails) -> tuple[set[str], set[str]]:
        self.calls.append((list(page_ids), list(emails)))
        return self.page_ids, self.emails

    def fetch_consents(self, page_ids) -> list[ConsentRecord]:
        self.consent_calls.append(list(page_ids))
        if self._consents is not None:
            return self._consents
        # 根拠は「登録時のアドレス＝今の宛先」でしか効かないので、
        # テストの連絡先が持つアドレスに合わせる。
        return [
            ConsentRecord(
                contact_page_id=page_id,
                contact_email="yamada@example.com",
                basis=BASIS_NOTIFIED,
                obtained_at=datetime.now(timezone.utc).date(),
                evidence="テスト用の根拠",
            )
            for page_id in page_ids
        ]


def _client_page(page_id: str = "cli-1", name: str = "テスト商事") -> dict[str, Any]:
    return {
        "id": page_id,
        "properties": {"取引先名": {"type": "title", "title": [{"plain_text": name}]}},
    }


def _contact_page(
    page_id: str = "cnt-1",
    name: str = "山田太郎",
    email: str | None = "yamada@example.com",
    department: str | None = "営業部",
    title: str | None = "部長",
) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "名前": {"type": "title", "title": [{"plain_text": name}]},
        "メールアドレス": {"type": "email", "email": email},
        "部署": {"type": "rich_text", "rich_text": [{"plain_text": department or ""}]},
        "役職": {"type": "rich_text", "rich_text": [{"plain_text": title or ""}]},
    }
    return {"id": page_id, "properties": properties}


def _data_source(
    *, contacts: list[dict[str, Any]] | None = None, clients: dict[str, dict[str, Any]] | None = None
) -> Client360DataSource:
    return Client360DataSource(
        client_master_client=_FakeClientMasterClient(clients or {"cli-1": _client_page()}),
        contact_client=_FakeQueryClient(contacts if contacts is not None else [_contact_page()]),
        project_client=_FakeQueryClient(),
        action_client=_FakeQueryClient(),
        user_directory=_FakeUserDirectory(),
        reply_timing_builder=lambda page_ids: {},
    )


@pytest.fixture(autouse=True)
def _sender_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """送信者情報と鍵を埋めた状態を既定にする（BLOCKERの有無で埋もれないように）。"""
    monkeypatch.setenv("BULK_EMAIL_SENDER_COMPANY_NAME", "テスト株式会社")
    monkeypatch.setenv("BULK_EMAIL_SENDER_POSTAL_CODE", "100-0001")
    monkeypatch.setenv("BULK_EMAIL_SENDER_ADDRESS", "東京都千代田区1-1-1")
    monkeypatch.setenv("BULK_EMAIL_SENDER_CONTACT_EMAIL", "info@example.com")
    monkeypatch.setenv("BULK_EMAIL_SENDER_CONTACT_URL", "https://example.com/")
    monkeypatch.setenv("BULK_EMAIL_UNSUBSCRIBE_SECRET", "test-secret")
    monkeypatch.setenv("DASHBOARD_BASE_URL", "https://dash.example.com")


def _preview(**kwargs: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "subject": "{{会社名}}様へ",
        "body": "{{会社名}}\n{{氏名}} 様",
        "client_page_ids": ["cli-1"],
        "sender_name": "金沢",
        "data_source": _data_source(),
        "preference_reader": _FakePreferenceReader(),
    }
    params.update(kwargs)
    return build_bulk_email_preview(**params)


def test_Notionの連絡先が宛先になる() -> None:
    result = _preview()
    assert result["sendable"] is True
    assert result["counts"] == {"sendable": 1, "skipped": 0}
    message = result["messages"][0]
    assert message["to_email"] == "yamada@example.com"
    assert message["client_name"] == "テスト商事"
    assert message["subject"] == "テスト商事様へ"


def test_配信停止の照合には宛先の候補だけを渡す() -> None:
    reader = _FakePreferenceReader()
    _preview(preference_reader=reader)
    assert reader.calls == [(["cnt-1"], ["yamada@example.com"])]


def test_配信停止の申し出がある宛先は外れる() -> None:
    result = _preview(preference_reader=_FakePreferenceReader(page_ids={"cnt-1"}))
    assert result["counts"] == {"sendable": 0, "skipped": 1}
    assert result["skipped"][0]["reason_label"] == "配信停止の申し出あり"


def test_配信停止が読めなかったら例外にする() -> None:
    """0人として続けると、止めた相手に送る形になるため握り潰さない。"""

    class _Broken:
        def fetch_opt_outs(self, page_ids, emails):
            raise RuntimeError("DBに繋がらない")

    with pytest.raises(RuntimeError):
        _preview(preference_reader=_Broken())


def test_同じ取引先を2回選んでも1回しか取りに行かない() -> None:
    reader = _FakePreferenceReader()
    result = _preview(client_page_ids=["cli-1", "cli-1"], preference_reader=reader)
    assert result["counts"]["sendable"] == 1


def test_取引先を選びすぎたら入力エラーにする() -> None:
    too_many = [f"cli-{i}" for i in range(bulk_email_service.MAX_CLIENTS_PER_PREVIEW + 1)]
    with pytest.raises(ValueError, match="一度に選べる取引先"):
        _preview(client_page_ids=too_many)


def test_見つからない取引先は警告に出す() -> None:
    result = _preview(client_page_ids=["cli-1", "ない"])
    assert any("見つかりませんでした" in warning for warning in result["warnings"])
    # 見つかった側の宛先は残る。
    assert result["counts"]["sendable"] == 1


def test_連絡先が上限まで返ってきたら打ち切りとして警告する() -> None:
    """送ったつもりで送っていない相手がいる、という形で表に出るため黙って捨てない。"""
    many = [_contact_page(f"cnt-{i}", email=f"c{i}@example.com") for i in range(100)]
    result = _preview(data_source=_data_source(contacts=many))
    assert any("先頭までしか読み込めていません" in w for w in result["warnings"])


def test_未入力のプロパティはNoneとして扱う() -> None:
    result = _preview(
        body="{{会社名}}\n{{部署}}",
        data_source=_data_source(contacts=[_contact_page(department="")]),
    )
    assert result["counts"] == {"sendable": 0, "skipped": 1}
    assert result["skipped"][0]["reason_label"] == "差し込む値が空"


def test_使える差し込み名の一覧を画面へ返す() -> None:
    names = [item["name"] for item in _preview()["placeholders_available"]]
    assert "会社名" in names and "担当者名" in names


def test_配信停止ページのURLは専用の環境変数を優先する(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DASHBOARD_BASE_URL", "https://dash.example.com/")
    monkeypatch.setenv("DASHBOARD_FRONTEND_ORIGIN", "https://other.example.com")
    assert unsubscribe_base_url() == "https://dash.example.com"


def test_専用の環境変数が無ければCORS設定の先頭を使う(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DASHBOARD_BASE_URL", raising=False)
    monkeypatch.setenv("DASHBOARD_FRONTEND_ORIGIN", " https://a.example.com , https://b.example.com")
    assert unsubscribe_base_url() == "https://a.example.com"


# ── 送ってよい根拠（オプトイン）の配線 ─────────────────────────────────


def test_根拠の照合にも宛先の候補だけを渡す() -> None:
    reader = _FakePreferenceReader()
    _preview(preference_reader=reader)
    assert reader.consent_calls == [["cnt-1"]]


def test_根拠が1件も無ければ宛先は0件になる() -> None:
    result = _preview(preference_reader=_FakePreferenceReader(consents=[]))
    assert result["counts"] == {"sendable": 0, "skipped": 1}
    assert result["skipped"][0]["reason"] == REASON_MISSING


def test_根拠が読めなかったら例外にする() -> None:
    """0件として続けると、設定漏れとDB障害が画面から区別できなくなる。"""

    class _Broken(_FakePreferenceReader):
        def fetch_consents(self, page_ids):
            raise RuntimeError("DBに繋がらない")

    with pytest.raises(RuntimeError):
        _preview(preference_reader=_Broken())


def _overview(**kwargs: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "client_page_ids": ["cli-1"],
        "data_source": _data_source(),
        "preference_reader": _FakePreferenceReader(consents=[]),
    }
    params.update(kwargs)
    return build_consent_overview(**params)


def test_根拠一覧は連絡先と今の状態を返す() -> None:
    result = _overview()
    assert result["counts"] == {"total": 1, "allowed": 0, "unsubscribed": 0}
    row = result["contacts"][0]
    assert row["contact_page_id"] == "cnt-1"
    # 登録時にどの取引先の下で確かめるか。名前ではなくIDで持ち回る。
    assert row["client_page_id"] == "cli-1"
    assert row["email"] == "yamada@example.com"
    assert row["consent"]["allowed"] is False
    assert row["consent"]["reason"] == REASON_MISSING
    # 画面が選択肢を組み立てられること。
    assert {option["value"] for option in result["basis_options"]} == {
        "opt_in",
        "notified",
        "transaction",
        "published",
    }


def test_根拠一覧では登録済みの内容がそのまま見える() -> None:
    consents = [
        ConsentRecord(
            contact_page_id="cnt-1",
            contact_email="yamada@example.com",
            basis=BASIS_NOTIFIED,
            obtained_at=date(2026, 4, 8),
            evidence="大阪ホテル展で名刺交換",
            recorded_by="kanazawa@cnctor.jp",
        )
    ]
    row = _overview(preference_reader=_FakePreferenceReader(consents=consents))["contacts"][0]
    assert row["consent"]["allowed"] is True
    assert row["consent"]["obtained_at"] == "2026-04-08"
    assert row["consent"]["evidence"] == "大阪ホテル展で名刺交換"
    assert row["consent"]["recorded_email"] == "yamada@example.com"


def test_根拠一覧は配信停止の相手も隠さない() -> None:
    """画面から消すと「なぜこの人だけ出てこないのか」が分からなくなる。"""
    reader = _FakePreferenceReader(page_ids={"cnt-1"}, consents=[])
    result = _overview(preference_reader=reader)
    assert result["contacts"][0]["unsubscribed"] is True
    assert result["counts"]["unsubscribed"] == 1


def test_根拠一覧でも取引先を選びすぎたら入力エラーにする() -> None:
    too_many = [f"cli-{i}" for i in range(bulk_email_service.MAX_CLIENTS_PER_PREVIEW + 1)]
    with pytest.raises(ValueError, match="一度に選べる取引先"):
        _overview(client_page_ids=too_many)


def test_他の連絡先の根拠はアドレスが同じでも借りられない() -> None:
    """代表アドレスの使い回しや、消えた連絡先の残骸で送れてしまうのを防ぐ。"""
    consents = [
        ConsentRecord(
            contact_page_id="ffffffffffffffffffffffffffffffff",
            contact_email="yamada@example.com",
            basis=BASIS_NOTIFIED,
            obtained_at=date(2026, 4, 8),
            evidence="大阪ホテル展で名刺交換",
        )
    ]
    row = _overview(preference_reader=_FakePreferenceReader(consents=consents))["contacts"][0]
    assert row["consent"]["allowed"] is False
    assert row["consent"]["reason"] == REASON_MISSING


def test_登録時とアドレスが変わっていたら送れないと出す() -> None:
    """名刺交換で得たのは「そのとき教えてもらったアドレス」。連絡先のアドレスが
    書き換わったら根拠は付いてこない（登録し直してもらう）。"""
    consents = [
        ConsentRecord(
            contact_page_id="cnt-1",
            contact_email="old@example.com",
            basis=BASIS_NOTIFIED,
            obtained_at=date(2026, 4, 8),
            evidence="大阪ホテル展で名刺交換",
        )
    ]
    row = _overview(preference_reader=_FakePreferenceReader(consents=consents))["contacts"][0]
    assert row["consent"]["allowed"] is False
    assert row["consent"]["reason"] == "consent_email_mismatch"
    assert row["consent"]["detail"] == "old@example.com"


def test_古さの表示文言はバックエンドが作る() -> None:
    row = _overview()["contacts"][0]
    assert row["consent"]["stale_label"] == "3年以上前"
