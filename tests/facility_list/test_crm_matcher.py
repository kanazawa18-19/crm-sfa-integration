"""CRM突合の検証。Notionにもローカルのインデックスにも実際には触らない。

`find_by_normalized_name`（実際のPostgresアクセス）はmonkeypatchで差し替える
（tests/relation_sync/test_resolve.py と同じ方針）。
"""

from __future__ import annotations

import pytest

from src.facility_list.domain.models import CrmMatchState, Facility, NameMatchStrength
from src.facility_list.infrastructure import crm_matcher as module
from src.facility_list.infrastructure.crm_matcher import CrmMatcher, facility_name_variants


@pytest.fixture(autouse=True)
def identity_index(monkeypatch):
    monkeypatch.setattr(module, "find_client_identity_candidates",
                        lambda addresses, phones: module.ClientIdentityIndex(complete=True))


@pytest.fixture
def facility() -> Facility:
    return Facility(hotel_no=1, name="皆生温泉　華水亭", prefecture="鳥取県", city="米子市", address="鳥取県米子市架空町1-2-3")


class TestFacilityNameVariants:
    def test_地名つきの施設名から施設名だけを取り出す(self) -> None:
        variants = facility_name_variants("皆生温泉　華水亭")
        names = [v for v, _ in variants]
        assert names[0] == "皆生温泉　華水亭"
        assert "華水亭" in names

    def test_最後の塊は弱い候補として返す(self) -> None:
        # ここを強い候補として扱うと、「ホテルABC 大阪」の「大阪」が別会社に当たり、
        # その会社の連絡先が別施設の行に出る(ChatGPTレビューのBLOCKER、2026-09-12)。
        variants = dict(facility_name_variants("ホテルABC　大阪"))
        assert variants["大阪"] is NameMatchStrength.WEAK
        assert variants["ホテルABC　大阪"] is NameMatchStrength.EXACT

    def test_先頭の修飾語を落とした形も試す(self) -> None:
        names = [v for v, _ in facility_name_variants("天然温泉　夕凪の湯")]
        assert "夕凪の湯" in names

    def test_連続した修飾語を全部落とす(self) -> None:
        # startswithのif1回だと先頭1つしか落ちない(Geminiレビュー指摘、2026-09-12)。
        variants = dict(facility_name_variants("天然温泉源泉かけ流し　夕凪の湯"))
        assert variants.get("夕凪の湯") is NameMatchStrength.STRIPPED

    def test_単一語ならそのままだけ(self) -> None:
        assert facility_name_variants("ハートンホテル西梅田") == [
            ("ハートンホテル西梅田", NameMatchStrength.EXACT)
        ]


class TestMatch:
    def test_1件だけ当たれば確定する(self, facility, monkeypatch) -> None:
        monkeypatch.setattr(
            module,
            "find_by_normalized_name",
            lambda name: [{"notion_page_id": "page-1", "raw_name": "株式会社華水亭"}]
            if "華水亭" in name
            else [],
        )
        matcher = CrmMatcher(notion_api_key=None)
        result = matcher.match(facility)
        assert result.state is CrmMatchState.MATCHED
        assert result.client_name == "株式会社華水亭"

    def test_当たらなければ名前照合なしとして返す(self, facility, monkeypatch) -> None:
        monkeypatch.setattr(module, "find_by_normalized_name", lambda name: [])
        result = CrmMatcher(notion_api_key=None).match(facility)
        assert result.state is CrmMatchState.NO_NAME_MATCH

    def test_弱い候補だけで当たっても住所の裏が取れなければ確定させない(
        self, facility, monkeypatch
    ) -> None:
        # 「皆生温泉　華水亭」の「華水亭」で1件当たっても、Notionを読めず
        # 都道府県を確認できないなら確定させない。
        monkeypatch.setattr(
            module,
            "find_by_normalized_name",
            lambda name: [{"notion_page_id": "page-x", "raw_name": "華水亭"}]
            if name == "華水亭"
            else [],
        )
        result = CrmMatcher(notion_api_key=None).match(facility)
        assert result.state is CrmMatchState.AMBIGUOUS

    def test_強い候補なら住所を確認しなくても確定する(self, facility, monkeypatch) -> None:
        monkeypatch.setattr(
            module,
            "find_by_normalized_name",
            lambda name: [{"notion_page_id": "page-1", "raw_name": "皆生温泉華水亭"}]
            if name == "皆生温泉華水亭"
            else [],
        )
        result = CrmMatcher(notion_api_key=None).match(facility)
        assert result.state is CrmMatchState.MATCHED
        assert result.matched_by is NameMatchStrength.EXACT

    def test_候補が複数なら確定させない(self, facility, monkeypatch) -> None:
        # Notionを読めない状態では都道府県で絞れないので、曖昧なまま返す。
        monkeypatch.setattr(
            module,
            "find_by_normalized_name",
            lambda name: [
                {"notion_page_id": "page-1", "raw_name": "株式会社華水亭"},
                {"notion_page_id": "page-2", "raw_name": "華水亭リゾート"},
            ],
        )
        result = CrmMatcher(notion_api_key=None).match(facility)
        assert result.state is CrmMatchState.AMBIGUOUS
        assert len(result.candidate_names) == 2


class TestMatchAll:
    def test_突合に失敗した施設を名前照合なしにしない(self, facility, monkeypatch) -> None:
        # ここがずれると、Notionが一時的に落ちただけで既存顧客が新規開拓リストへ
        # 混ざる(obasan-qualityレビューのBLOCKER、2026-09-11)。
        def _boom(name: str):  # noqa: ANN202
            raise RuntimeError("Postgresに接続できない")

        monkeypatch.setattr(module, "find_client_pages_by_normalized_names", _boom)
        results = CrmMatcher(notion_api_key=None).match_all([facility])
        assert results[facility.hotel_no].state is CrmMatchState.NOT_CHECKED
        assert results[facility.hotel_no].state is not CrmMatchState.NO_NAME_MATCH

    def test_1件の失敗で全体を止めない(self, monkeypatch) -> None:
        good = Facility(hotel_no=1, name="架空の宿", address="鳥取県米子市架空町1-2-3")
        bad = Facility(hotel_no=2, name="壊れる宿")
        monkeypatch.setattr(module, "find_client_pages_by_normalized_names", lambda names: {})
        matcher = CrmMatcher(notion_api_key="")
        original = matcher._match

        def sometimes(facility):
            if facility.hotel_no == 2:
                raise RuntimeError("失敗")
            return original(facility)

        monkeypatch.setattr(matcher, "_match", sometimes)
        results = matcher.match_all([good, bad])
        assert results[1].state is CrmMatchState.NO_NAME_MATCH
        assert results[2].state is CrmMatchState.NOT_CHECKED
        assert matcher.unchecked_count == 1


def test_APIキーが無ければNotionを読まないと自己申告する() -> None:
    assert CrmMatcher(notion_api_key=None).has_notion_access is False
    assert CrmMatcher(notion_api_key="key").has_notion_access is True


class TestTimeBudget:
    """時間予算を使い切ったら打ち切る(Vercelの300秒に対する構造的な備え)。"""

    def test_予算切れなら未突合にして打ち切る(self, monkeypatch) -> None:
        def _never_called(name: str):  # noqa: ANN202
            raise AssertionError("予算切れなのに照合しようとした")

        monkeypatch.setattr(module, "find_by_normalized_name", _never_called)
        monkeypatch.setattr(
            module, "find_client_pages_by_normalized_names", lambda names: {}
        )
        matcher = CrmMatcher(notion_api_key=None, time_budget_seconds=0.0)
        results = matcher.match_all([Facility(hotel_no=1, name="テスト旅館")])
        # **NO_NAME_MATCH(＝新規リストに載る)にしない。**
        assert results[1].state is CrmMatchState.NOT_CHECKED
        assert matcher.skipped_by_budget == 1

    def test_予算内なら普通に突合する(self, monkeypatch) -> None:
        monkeypatch.setattr(module, "find_by_normalized_name", lambda name: [])
        monkeypatch.setattr(
            module, "find_client_pages_by_normalized_names", lambda names: {}
        )
        matcher = CrmMatcher(notion_api_key=None, time_budget_seconds=60.0)
        results = matcher.match_all([Facility(hotel_no=1, name="テスト旅館")])
        assert results[1].state is CrmMatchState.NOT_CHECKED
        assert matcher.skipped_by_budget == 0
