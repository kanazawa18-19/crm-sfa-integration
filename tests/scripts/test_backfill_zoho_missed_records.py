"""scripts/backfill_zoho_missed_records.py の単体テスト。
Zoho・Notion・Postgres へは一切到達させない（クライアント・Dispatcher はすべて偽物）。"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "backfill_zoho_missed_records.py"
_spec = importlib.util.spec_from_file_location("backfill_zoho_missed_records", _SCRIPT)
mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = mod
_spec.loader.exec_module(mod)

SINCE = datetime(2026, 9, 15, 16, 0, tzinfo=timezone.utc)


# --- 純粋な判定 ------------------------------------------------------------------------------


def test_split_by_mapping_marks_unmapped_as_new_and_mapped_as_existing() -> None:
    items = mod.split_by_mapping(["1", "2", 3], {"2": "notion-page-2"}, "contact")

    assert [(it.zoho_id, it.mapped, it.notion_key) for it in items] == [
        ("1", False, None),
        ("2", True, "notion-page-2"),
        ("3", False, None),
    ]
    # 既存は最初から「今回は触らない」に分類される。新規は予測前なので未分類
    assert items[1].predicted == mod.PREDICT_SKIP_MAPPED
    assert items[0].predicted == ""


def test_missing_required_properties_uses_contact_schema_requirements() -> None:
    """連絡先の必須は「名前」だけ。「取引先マスター」は 2026-09-26 に任意へ変えた
    （Zoho の「お取引先」が空でも作る、本人判断）ので、空でも不足として出ないこと。"""
    assert mod.missing_required_properties("contact", {"名前": "山田 太郎"}) == []
    assert mod.missing_required_properties("contact", {}) == ["名前"]


def test_parse_since_requires_timezone() -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        mod.parse_since("2026-09-15T16:00")
    assert mod.parse_since("2026-09-15T16:00+09:00").utcoffset() is not None


def test_zoho_record_label_uses_lookup_name_for_account() -> None:
    assert mod.zoho_record_label({"Full_Name": "山田 太郎", "field25": {"name": "株式会社テスト", "id": "9"}}) == "山田 太郎 | 株式会社テスト"
    assert mod.zoho_record_label({"Full_Name": "山田"}) == "山田 | -"


def test_parse_args_defaults_to_dry_run() -> None:
    args = mod.parse_args(["--since", "2026-09-15T16:00+09:00"])
    assert args.apply is False
    assert args.modules is None
    assert args.limit is None


def test_modules_to_db_keys_rejects_unknown_module() -> None:
    assert mod.modules_to_db_keys(["Contacts"]) == {"Contacts": "contact"}
    assert set(mod.modules_to_db_keys(None)) == {"Deals", "CustomModule3", "CustomModule2", "Accounts", "Contacts", "Products"}
    with pytest.raises(SystemExit):
        mod.modules_to_db_keys(["Leads"])


def test_zoho_v3_base_url_is_derived_from_v2_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZOHO_API_BASE_URL", "https://www.zohoapis.jp/crm/v2")
    assert mod.zoho_v3_base_url() == "https://www.zohoapis.jp/crm/v3"
    monkeypatch.delenv("ZOHO_API_BASE_URL")
    assert mod.zoho_v3_base_url() == "https://www.zohoapis.jp/crm/v3"


# --- load_env の安全装置 ------------------------------------------------------------------------


_ENV_KEYS = ("NOTION_API_KEY", "ZOHO_CLIENT_ID", "ZOHO_CLIENT_SECRET", "ZOHO_REFRESH_TOKEN", "DATABASE_URL",
             "SYNC_ID_MAPPING_BACKEND", "SYNC_ID_MAPPING_NOTION_API_KEY", "ENABLE_ZOHO",
             "AUTO_CREATE_NEW_RECORDS_ENABLED", "RELATION_SYNC_ENABLED",
             "SPREADSHEET_ROW_CREATION_ENABLED", "SPREADSHEET_ROW_CREATION_DB_KEYS", "DATABASE_URL_UNPOOLED")


@pytest.fixture
def env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # load_env() は os.environ を直接書くので、元の状態（無かったことも含めて）を monkeypatch に
    # 記録させて後片付けさせる（setenv→delenv で「元は無かった」が記録される）
    for k in _ENV_KEYS:
        monkeypatch.setenv(k, "")
        monkeypatch.delenv(k)
    p = tmp_path / ".env"
    p.write_text(
        "NOTION_API_KEY=n\nZOHO_CLIENT_ID=c\nZOHO_CLIENT_SECRET=s\nZOHO_REFRESH_TOKEN=r\n"
        "ENABLE_ZOHO=False\nSYNC_ID_MAPPING_BACKEND=sqlite\n",
        encoding="utf-8",
    )
    return p


def test_load_env_forces_notion_mapping_backend_and_no_writes_in_dry_run(env_file: Path) -> None:
    import os

    mod.load_env(apply=False, env_path=env_file)

    # .env に sqlite と書いてあっても本番と同じ Notion 対応表を強制する（二重作成防止）
    assert os.environ["SYNC_ID_MAPPING_BACKEND"] == "notion"
    assert os.environ["SYNC_ID_MAPPING_NOTION_API_KEY"] == "n"
    assert os.environ["ENABLE_ZOHO"] == "True"
    # dry-run ではレビューキューへの書き込みも自動作成もシート行作成も起こさない
    assert os.environ["RELATION_SYNC_ENABLED"] == "false"
    assert "AUTO_CREATE_NEW_RECORDS_ENABLED" not in os.environ
    assert "SPREADSHEET_ROW_CREATION_ENABLED" not in os.environ


def test_load_env_apply_requires_database_url(env_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    with pytest.raises(SystemExit) as exc:
        mod.load_env(apply=True, env_path=env_file)
    assert exc.value.code == 2

    monkeypatch.setenv("DATABASE_URL", "postgres://x")
    mod.load_env(apply=True, env_path=env_file)
    assert os.environ["AUTO_CREATE_NEW_RECORDS_ENABLED"] == "true"
    assert os.environ["RELATION_SYNC_ENABLED"] == "true"
    # シートの行もその場で作る（対象 db_key は全部）
    assert os.environ["SPREADSHEET_ROW_CREATION_ENABLED"] == "true"
    assert os.environ["SPREADSHEET_ROW_CREATION_DB_KEYS"] == "*"


def test_load_env_fails_when_required_credentials_are_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for k in _ENV_KEYS:
        monkeypatch.setenv(k, "")
        monkeypatch.delenv(k)
    p = tmp_path / ".env"
    p.write_text("NOTION_API_KEY=n\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        mod.load_env(apply=False, env_path=p)


# --- Zoho 一覧（カーソル方式） ------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int, body=None):
        self.status_code = status_code
        self._body = body
        self.text = ""

    @property
    def ok(self) -> bool:
        return self.status_code < 400

    def json(self):
        return self._body


class _FakeZoho:
    """`HttpZohoClient.request()` / `get_record()` / `_headers()` の偽物。"""

    def __init__(self, pages, records=None):
        self.pages = list(pages)
        self.urls: list[str] = []
        self.records = records or {}

    def _headers(self):
        return {"Authorization": "Zoho-oauthtoken x"}

    def request(self, method, url, *, json_body=None, idempotent=True):
        assert method == "GET"
        self.urls.append(url)
        return self.pages.pop(0)

    def get_record(self, module, record_id):
        return self.records.get(record_id)


def test_fetch_zoho_ids_follows_page_token_until_more_records_is_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZOHO_API_BASE_URL", "https://www.zohoapis.jp/crm/v2")
    zoho = _FakeZoho([
        _FakeResponse(200, {"data": [{"id": "1"}, {"id": "2"}], "info": {"more_records": True, "next_page_token": "tok2"}}),
        _FakeResponse(200, {"data": [{"id": "3"}], "info": {"more_records": False}}),
    ])
    monkeypatch.setattr(mod.time, "sleep", lambda *_: None)

    ids = mod.fetch_zoho_ids_modified_since(zoho, "Contacts", SINCE)

    assert ids == ["1", "2", "3"]
    assert zoho.urls[0].startswith("https://www.zohoapis.jp/crm/v3/Contacts?fields=id&per_page=200")
    assert "page_token=tok2" in zoho.urls[1]
    assert "&page=" not in zoho.urls[0] and "?page=" not in zoho.urls[0]  # v2 の page 方式（2,000 件上限）は使わない


def test_fetch_zoho_ids_returns_empty_on_304_and_204(monkeypatch: pytest.MonkeyPatch) -> None:
    assert mod.fetch_zoho_ids_modified_since(_FakeZoho([_FakeResponse(304)]), "Contacts", SINCE) == []
    assert mod.fetch_zoho_ids_modified_since(_FakeZoho([_FakeResponse(204)]), "Contacts", SINCE) == []


def test_fetch_zoho_ids_raises_on_http_error() -> None:
    from src.sync_engine.clients.zoho_client import ZohoApiError

    with pytest.raises(ZohoApiError):
        mod.fetch_zoho_ids_modified_since(_FakeZoho([_FakeResponse(400, {"code": "INVALID_DATA"})]), "Contacts", SINCE)


def test_with_if_modified_since_adds_header_and_can_be_restored() -> None:
    zoho = _FakeZoho([])
    original = mod._with_if_modified_since(zoho, SINCE.replace(microsecond=123456))
    assert zoho._headers()["If-Modified-Since"] == "2026-09-15T16:00:00+00:00"  # 秒精度に丸める
    zoho._headers = original
    assert "If-Modified-Since" not in zoho._headers()


# --- dry-run の予測 ------------------------------------------------------------------------------


def _contact_item() -> "mod.Classified":
    return mod.Classified(zoho_id="10", db_key="contact", mapped=False)


def test_predict_creation_marks_create_when_lookup_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "src.sync_engine.new_record_builder.build_notion_properties_for_new_record",
        lambda **kw: {"名前": "山田 太郎"},
    )
    it = _contact_item()
    mod.predict_creation(it, {"Full_Name": "山田 太郎", "field25": {"name": "株式会社テスト"}}, store=None,
                         lookup=lambda v: ("page-1", True))
    assert it.predicted == mod.PREDICT_CREATE
    assert it.missing_required == []
    assert it.label == "山田 太郎 | 株式会社テスト"


def test_predict_creation_marks_missing_required_when_lookup_finds_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "src.sync_engine.new_record_builder.build_notion_properties_for_new_record",
        lambda **kw: {"名前": "山田 太郎"},
    )
    it = _contact_item()
    mod.predict_creation(it, {"Full_Name": "山田 太郎"}, store=None, lookup=lambda v: (None, True))
    # 取引先マスターは任意（2026-09-26）なので、解決できなくても作れる
    assert it.predicted == mod.PREDICT_CREATE
    assert it.missing_required == []


def test_predict_creation_marks_unverified_when_lookup_could_not_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """DB に繋げなかったレコードは「未検証」であって「必須不足」ではない。しかも 1 件ごとに
    判定するので、前のレコードで失敗しても次のレコードの判定には影響しない。"""
    monkeypatch.setattr(
        "src.sync_engine.new_record_builder.build_notion_properties_for_new_record",
        lambda **kw: {"名前": "山田 太郎"},
    )
    calls = iter([(None, False), ("page-2", True)])
    first, second = _contact_item(), _contact_item()
    mod.predict_creation(first, {"Full_Name": "A"}, store=None, lookup=lambda v: next(calls))
    mod.predict_creation(second, {"Full_Name": "B"}, store=None, lookup=lambda v: next(calls))
    # 取引先マスターが任意になった今は、未検証でも「作れる」に倒れる（必須項目は名前だけ）
    assert first.predicted == mod.PREDICT_CREATE
    assert second.predicted == mod.PREDICT_CREATE


def test_lookup_client_master_readonly_without_database_url_is_unverified(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert mod.lookup_client_master_readonly({"name": "株式会社テスト"}) == (None, False)


def test_lookup_client_master_readonly_treats_db_error_per_call(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgres://x")
    results = iter([RuntimeError("down"), [{"notion_page_id": "page-9"}]])

    def fake_find(_normalized):
        r = next(results)
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr("src.relation_sync.db.find_by_normalized_name", fake_find)
    assert mod.lookup_client_master_readonly({"name": "株式会社テスト"}) == (None, False)
    # 直前の失敗を引きずらず、次の呼び出しでは解決できる
    assert mod.lookup_client_master_readonly({"name": "株式会社テスト"}) == ("page-9", True)
    # 未入力は「解決対象外」だが検証はできている
    assert mod.lookup_client_master_readonly(None) == (None, True)


def test_dry_run_predict_records_fetch_failures_and_continues(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod, "predict_creation", lambda it, raw, store, **kw: setattr(it, "predicted", mod.PREDICT_CREATE))
    monkeypatch.setattr(mod.time, "sleep", lambda *_: None)
    zoho = _FakeZoho([], records={"2": {"Full_Name": "B"}})
    items = [mod.Classified("1", "contact", False), mod.Classified("2", "contact", False)]

    mod.dry_run_predict(zoho, None, items)

    assert items[0].predicted == mod.PREDICT_FETCH_FAILED
    assert items[1].predicted == mod.PREDICT_CREATE


# --- --apply: 予測で絞らず全件を Dispatcher に流し、1 件の失敗で止めない ------------------------------


class _FakeDispatcher:
    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.events = []

    def dispatch(self, event):
        self.events.append(event)
        outcome = self.outcomes[event.external_id]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_apply_all_dispatches_every_new_record_and_continues_after_errors() -> None:
    dispatcher = _FakeDispatcher({
        "1": SimpleNamespace(skipped=False, reason=None),
        "2": SimpleNamespace(skipped=True, reason="new_record_missing_required_properties"),
        "3": RuntimeError("boom"),
    })
    items = [mod.Classified(z, "contact", False) for z in ("1", "2", "3")]

    counts = mod.apply_all(dispatcher, items, sleep_seconds=0)

    assert [e.external_id for e in dispatcher.events] == ["1", "2", "3"]
    assert [it.dispatch_reason for it in items] == ["created", "new_record_missing_required_properties", "error"]
    assert items[2].error is not None
    assert counts == {"created": 1, "new_record_missing_required_properties": 1, "error": 1}
    event = dispatcher.events[0]
    assert event.source_tool.value == "zoho"
    assert event.db_key == "contact" and event.properties == {}


def test_build_production_dispatcher_accepts_explicit_none_notifier(monkeypatch: pytest.MonkeyPatch) -> None:
    """回収スクリプトが Slack 通知を切れること（省略時は従来どおり運用 DM を使う）。"""
    from src.sync_engine import production_wiring as pw

    monkeypatch.setattr(pw, "build_notion_clients_by_db", lambda **_: {})
    monkeypatch.setattr(pw, "build_kintone_targets_by_db", lambda: {})
    monkeypatch.setattr(pw, "build_zoho_targets_by_db", lambda _c=None: {})
    monkeypatch.setattr(pw, "build_spreadsheet_targets_by_db", lambda: {})

    class _Store:  # IdMappingStore の代わり（呼ばれない）
        pass

    silent = pw.build_production_dispatcher(id_mapping_store=_Store(), slack_notifier=None)
    assert silent._slack_notifier is None
    default = pw.build_production_dispatcher(id_mapping_store=_Store())
    assert isinstance(default._slack_notifier, pw.WebhookSlackNotifier)
    custom = object()
    assert pw.build_production_dispatcher(id_mapping_store=_Store(), slack_notifier=custom)._slack_notifier is custom


def test_apply_all_skips_records_mapped_after_collection() -> None:
    """収集後に本番 Webhook が同じレコードを作成済み（対応表に載った）なら、Dispatcher に流さない。"""
    dispatcher = _FakeDispatcher({"2": SimpleNamespace(skipped=False, reason=None)})

    class _Store:
        def find_by_external_id(self, tool, external_id, *, db_key):
            return SimpleNamespace(notion_key="page-1") if external_id == "1" else None

    items = [mod.Classified("1", "contact", False), mod.Classified("2", "contact", False)]
    counts = mod.apply_all(dispatcher, items, store=_Store(), sleep_seconds=0)

    assert [e.external_id for e in dispatcher.events] == ["2"]
    assert items[0].dispatch_reason == "mapped_meanwhile" and items[0].notion_key == "page-1"
    assert counts == {"mapped_meanwhile": 1, "created": 1}


def test_exit_code_reflects_errors_and_skips() -> None:
    assert mod.exit_code_for({"created": 3}) == 0
    assert mod.exit_code_for({"created": 3, "mapped_meanwhile": 1}) == 0
    assert mod.exit_code_for({"created": 3, "new_record_missing_required_properties": 1}) == 3
    assert mod.exit_code_for({"created": 3, "error": 1, "new_record_missing_required_properties": 1}) == 1
