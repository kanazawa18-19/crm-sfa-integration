"""二次照合の事故防止。データはすべて架空、Notion通信は代替応答。"""

from dataclasses import replace
from decimal import Decimal

import pytest

from src.facility_list.domain.identity import normalize_address, normalize_phone
from src.facility_list.domain.models import CrmMatchState, Facility, CrmFilter, ListCriteria
from src.facility_list.domain.services import matches_criteria
from src.facility_list.infrastructure import crm_matcher as module
from src.relation_sync.sync import _page_to_index_row


ADDRESS = "鳥取県米子市架空町1-2-3"
PHONE = "0859-00-0001"


@pytest.mark.parametrize("value", ["０８５９（００）０００１", "+81-859-00-0001", PHONE])
def test_電話の表記を揃える(value):
    assert normalize_phone(value) == "0859000001"


@pytest.mark.parametrize("value", [None, "", "0000000000", "0859-00-0001 内線10", "0859-00-0001/0002"])
def test_不完全な電話を推測しない(value):
    assert normalize_phone(value) is None


@pytest.mark.parametrize("value,pref", [
    ("〒000-0000 鳥取県 米子市 架空町１丁目２番３号", None),
    ("米子市架空町1−2−3", "鳥取県"), (ADDRESS, None),
])
def test_番地の表記を揃える(value, pref):
    assert normalize_address(value, pref) == ADDRESS


def test_住所の不足と部屋違いを同一視しない():
    assert normalize_address("米子市", "鳥取県") is None
    assert normalize_address("米子市架空町1-2-3", "鳥取") is None
    assert normalize_address(ADDRESS + "101号室") != normalize_address(ADDRESS + "102号室")
    assert normalize_address(ADDRESS + "0") != normalize_address(ADDRESS)


def _text(value, kind="rich_text"):
    return {"type": kind, kind: [{"plain_text": value}]}


def _page(address=ADDRESS, phone=PHONE):
    return {"id": "page-a", "properties": {
        "取引先名": _text("架空運営株式会社", "title"),
        "住所": _text(address),
        "TEL": {"type": "phone_number", "phone_number": phone},
        "都道府県": {"type": "select", "select": {"name": "鳥取県"}},
    }}


def test_同期が取得済みの住所電話を保存し欠落と区別する():
    row = _page_to_index_row(_page())
    assert row["normalized_address"] == ADDRESS
    assert row["normalized_phone"] == "0859000001"
    assert row["identity_checked"] is True
    empty = _page_to_index_row(_page("", None))
    assert empty["identity_checked"] is True
    assert empty["normalized_address"] is None
    page = _page()
    del page["properties"]["TEL"]
    assert not _page_to_index_row(page).get("identity_checked")


@pytest.fixture
def setup(monkeypatch):
    row = {"notion_page_id": "page-a", "raw_name": "架空運営株式会社",
           "address": ADDRESS, "phone": "0859000001"}
    state = {"rows": (row,), "complete": True, "names": [], "calls": 0}

    def lookup(addresses, phones):
        state["calls"] += 1
        return module.ClientIdentityIndex(rows=state["rows"], complete=state["complete"])

    monkeypatch.setattr(module, "find_client_identity_candidates", lookup)
    monkeypatch.setattr(module, "find_by_normalized_name", lambda name: state["names"])
    monkeypatch.setattr(module, "find_client_pages_by_normalized_names", lambda names: {})
    matcher = module.CrmMatcher(notion_api_key="fixture")
    monkeypatch.setattr(matcher, "_load_client_page", lambda page_id: _page())
    monkeypatch.setattr(matcher, "_load_contacts", lambda page_id: ())
    return matcher, Facility(hotel_no=1, name="架空の宿", address=ADDRESS,
                             prefecture="鳥取県", telephone=PHONE, room_count=20, review_average=Decimal("4.0")), state


def test_名前の違う運営会社を住所電話で確定する(setup):
    matcher, facility, _ = setup
    result = matcher.match(facility)
    assert result.state is CrmMatchState.MATCHED
    assert result.client_name == "架空運営株式会社"
    assert result.evidence == ("住所一致", "電話一致")
    assert result.matched_by is None


@pytest.mark.parametrize("missing", ["address", "telephone"])
def test_単独一致は新規から除外し連絡先を開示しない(setup, missing):
    matcher, facility, _ = setup
    facility = replace(facility, **{missing: None})
    result = matcher.match(facility)
    assert result.state is CrmMatchState.AMBIGUOUS
    assert not matches_criteria(facility, ListCriteria(crm_filter=CrmFilter.NEW_ONLY), result)
    assert result.contacts == () and result.client_phone is None


def test_同じ住所の複数社は電話が片方でも保留する(setup):
    matcher, facility, state = setup
    state["rows"] += ({**state["rows"][0], "notion_page_id": "page-b", "phone": "0859000002"},)
    assert matcher.match(facility).state is CrmMatchState.AMBIGUOUS


def test_名前と住所が違う会社を指したら保留する(setup):
    matcher, facility, state = setup
    state["names"] = [{"notion_page_id": "page-b", "raw_name": "別の架空会社"}]
    assert matcher.match(facility).state is CrmMatchState.AMBIGUOUS


def test_住所が同じでも電話矛盾なら名前候補があっても保留する(setup):
    matcher, facility, state = setup
    state["names"] = [state["rows"][0]]
    assert matcher.match(replace(facility, telephone="0859-00-0002")).state is CrmMatchState.AMBIGUOUS


def test_ミラー取得途中の不一致は未突合にする(setup):
    matcher, facility, state = setup
    state.update(rows=(), complete=False)
    result = matcher.match(facility)
    assert result.state is CrmMatchState.NOT_CHECKED
    assert not matches_criteria(facility, ListCriteria(crm_filter=CrmFilter.NEW_ONLY), result)


def test_二次照合済みの不一致だけを新規候補にする(setup):
    matcher, facility, state = setup
    state["rows"] = ()
    result = matcher.match(facility)
    assert result.state is CrmMatchState.NO_NAME_MATCH
    assert matches_criteria(facility, ListCriteria(crm_filter=CrmFilter.NEW_ONLY), result)
    assert matcher.match(replace(facility, address=None, telephone=None)).state is CrmMatchState.NOT_CHECKED


def test_Notionの現行値が変わっていたら確定しない(setup, monkeypatch):
    matcher, facility, _ = setup
    monkeypatch.setattr(matcher, "_load_client_page", lambda page_id: _page(phone="0859-00-0002"))
    assert matcher.match(facility).state is CrmMatchState.AMBIGUOUS


def test_ミラー未記録の電話でも現行値の矛盾を見逃さない(setup, monkeypatch):
    matcher, facility, state = setup
    state["rows"] = ({**state["rows"][0], "phone": None},)
    state["names"] = [state["rows"][0]]
    monkeypatch.setattr(matcher, "_load_client_page", lambda page_id: _page(phone="0859-00-0002"))
    result = matcher.match(facility)
    assert result.state is CrmMatchState.AMBIGUOUS
    assert result.contacts == () and result.client_phone is None


def test_候補の取得順で照合根拠が変わらない(setup):
    matcher, facility, state = setup
    state["rows"] = (
        {**state["rows"][0], "phone": None},
        {**state["rows"][0], "notion_page_id": "page-b", "raw_name": "架空B社", "address": None},
    )
    before = matcher.match(facility)
    state["rows"] = tuple(reversed(state["rows"]))
    after = matcher.match(facility)
    assert before.evidence == after.evidence
    assert "架空運営株式会社：住所一致" in before.evidence
    assert "架空B社：電話一致" in before.evidence


def test_要確認で除外した件数を出力件数と別に返す(setup):
    from src.facility_list.application.build_list import build_list
    matcher, facility, _ = setup
    result = build_list(ListCriteria(crm_filter=CrmFilter.NEW_ONLY), matcher=matcher,
                        facilities=[replace(facility, telephone=None)])
    assert result.total == 0
    assert result.ambiguous_count == 0
    assert result.excluded_ambiguous_count == 1


def test_候補のページ取得失敗は新規にしない(setup, monkeypatch):
    matcher, facility, _ = setup
    def fail(page_id):
        raise RuntimeError("失敗")
    monkeypatch.setattr(matcher, "_load_client_page", fail)
    assert matcher.match(facility).state is CrmMatchState.NOT_CHECKED


def test_一括取得は一度で失敗と予算切れを集計する(setup, monkeypatch):
    matcher, facility, state = setup
    state.update(rows=(), complete=False)
    result = matcher.match_all([facility, replace(facility, hotel_no=2)])
    assert state["calls"] == 1
    assert matcher.unchecked_count == 2
    assert matcher.skipped_by_budget == 0
    assert all(r.state is CrmMatchState.NOT_CHECKED for r in result.values())


def test_1施設の途中で予算が切れたら結果を確定しない(setup, monkeypatch):
    matcher, facility, _ = setup
    clock = [0.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    def slow(page_id):
        clock[0] = 181.0
        return _page()
    monkeypatch.setattr(matcher, "_load_client_page", slow)
    result = matcher.match_all([facility, replace(facility, hotel_no=2)])
    assert all(r.state is CrmMatchState.NOT_CHECKED for r in result.values())
    assert matcher.skipped_by_budget == matcher.unchecked_count == 2


def test_二次取得で予算が切れたら名前検索を始めない(setup, monkeypatch):
    matcher, facility, _ = setup
    clock = [0.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    def slow(addresses, phones):
        clock[0] = 181.0
        return module.ClientIdentityIndex(complete=True)
    def forbidden(names):
        pytest.fail("予算切れ後に名前検索した")
    monkeypatch.setattr(module, "find_client_identity_candidates", slow)
    monkeypatch.setattr(module, "find_client_pages_by_normalized_names", forbidden)
    assert matcher.match_all([facility])[1].state is CrmMatchState.NOT_CHECKED
    assert matcher.skipped_by_budget == 1


def test_連絡先は確定取引先だけページングし再利用する(monkeypatch):
    matcher = module.CrmMatcher(notion_api_key="fixture")
    bodies = []
    class ContactClient:
        def query_raw(self, body):
            bodies.append(dict(body))
            return {"results": [], "has_more": len(bodies) == 1,
                    "next_cursor": "next" if len(bodies) == 1 else None}
    monkeypatch.setattr(matcher, "_contact_db", lambda: ContactClient())
    assert matcher._load_contacts("page-a") == ()
    assert matcher._load_contacts("page-a") == ()
    assert len(bodies) == 2
    assert bodies[0]["filter"] == {"property": "取引先マスター", "relation": {"contains": "page-a"}}
    assert bodies[1]["start_cursor"] == "next"


def test_個別通信に残り時間を渡し自動再試行を止める(monkeypatch):
    matcher = module.CrmMatcher(notion_api_key="fixture")
    matcher._deadline = 105.0
    monkeypatch.setattr(module.time, "monotonic", lambda: 100.0)
    assert matcher._request_limits() == {"timeout": 5.0, "max_retries": 0, "max_rate_limit_retries": 0}


def test_単発の名前検索途中で予算が切れたら次の表記を検索しない(setup, monkeypatch):
    matcher, facility, state = setup
    state["rows"] = ()
    clock = [0.0]
    calls = []
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    def slow(name):
        calls.append(name)
        clock[0] = 181.0
        return []
    monkeypatch.setattr(module, "find_by_normalized_name", slow)
    assert matcher.match(replace(facility, name="天然温泉　架空の宿")).state is CrmMatchState.NOT_CHECKED
    assert len(calls) == 1
