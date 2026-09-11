"""CRM突合の検証。Notionにもローカルのインデックスにも実際には触らない。

`find_by_normalized_name`（実際のPostgresアクセス）はmonkeypatchで差し替える
（tests/relation_sync/test_resolve.py と同じ方針）。
"""

from __future__ import annotations

import pytest

from src.facility_list.domain.models import CrmMatchState, Facility
from src.facility_list.infrastructure import crm_matcher as module
from src.facility_list.infrastructure.crm_matcher import CrmMatcher, facility_name_variants


@pytest.fixture
def facility() -> Facility:
    return Facility(hotel_no=1, name="皆生温泉　華水亭", prefecture="鳥取県", city="米子市")


class TestFacilityNameVariants:
    def test_地名つきの施設名から施設名だけを取り出す(self) -> None:
        variants = facility_name_variants("皆生温泉　華水亭")
        assert variants[0] == "皆生温泉　華水亭"
        assert "華水亭" in variants

    def test_先頭の修飾語を落とした形も試す(self) -> None:
        assert "夕凪の湯" in " ".join(facility_name_variants("天然温泉　夕凪の湯"))

    def test_単一語ならそのままだけ(self) -> None:
        assert facility_name_variants("ハートンホテル西梅田") == ["ハートンホテル西梅田"]


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

    def test_当たらなければ未取引(self, facility, monkeypatch) -> None:
        monkeypatch.setattr(module, "find_by_normalized_name", lambda name: [])
        result = CrmMatcher(notion_api_key=None).match(facility)
        assert result.state is CrmMatchState.NOT_FOUND

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
    def test_突合に失敗した施設を未取引にしない(self, facility, monkeypatch) -> None:
        # ここがずれると、Notionが一時的に落ちただけで既存顧客が新規開拓リストへ
        # 混ざる(obasan-qualityレビューのBLOCKER、2026-09-11)。
        def _boom(name: str):  # noqa: ANN202
            raise RuntimeError("Postgresに接続できない")

        monkeypatch.setattr(module, "find_by_normalized_name", _boom)
        results = CrmMatcher(notion_api_key=None).match_all([facility])
        assert results[facility.hotel_no].state is CrmMatchState.NOT_CHECKED
        assert results[facility.hotel_no].state is not CrmMatchState.NOT_FOUND

    def test_1件の失敗で全体を止めない(self, monkeypatch) -> None:
        good = Facility(hotel_no=1, name="華水亭", prefecture="鳥取県")
        bad = Facility(hotel_no=2, name="壊れる宿", prefecture="鳥取県")

        def _sometimes(name: str):  # noqa: ANN202
            if "壊れる" in name:
                raise RuntimeError("失敗")
            return []

        monkeypatch.setattr(module, "find_by_normalized_name", _sometimes)
        results = CrmMatcher(notion_api_key=None).match_all([good, bad])
        assert results[1].state is CrmMatchState.NOT_FOUND
        assert results[2].state is CrmMatchState.NOT_CHECKED


def test_APIキーが無ければNotionを読まないと自己申告する() -> None:
    assert CrmMatcher(notion_api_key=None).has_notion_access is False
    assert CrmMatcher(notion_api_key="key").has_notion_access is True
