"""取り込みユースケース（src/facility_list/application/import_facilities.py）の検証。

楽天へは一切アクセスしない。`RakutenTravelClient`の役だけを果たすフェイクを渡す。
"""

from __future__ import annotations

import pytest

from src.facility_list.application.import_facilities import (
    ImportResult,
    enrich_facilities,
    enrich_facility,
    import_prefecture_shallow,
)
from src.facility_list.domain.models import CustomPageStatus, Facility, FacilityCategory
from src.facility_list.infrastructure.rakuten_client import (
    RakutenFetchError,
    RobotsDisallowedError,
)
from src.facility_list.infrastructure.rakuten_page_parser import AreaListEntry

TOP_HTML = """
<html><head><title>テスト旅館 宿泊予約【楽天トラベル】</title></head><body>
<meta property="ratingValue" content="4.5">
<a href="x" property="reviewCount" content="120">(120件)</a>
<a href="gallery.html">写真・動画(48)</a>
<a href="GW9991234.html">お知らせ</a>
</body></html>
""" + "<!-- padding -->" * 200

DETAIL_HTML = """
<html><body><table>
<tr><th>総部屋数</th><td>42室</td></tr>
<tr><th>館内設備</th><td><ul><li>大浴場</li></ul></td></tr>
<tr><th>部屋設備・備品</th><td><ul><li>浴衣</li></ul></td></tr>
<tr><th>風呂</th><td>[種類] 温泉&nbsp;</td></tr>
<tr><th>住所</th><td>〒680-0000鳥取県鳥取市1-1</td></tr>
</table></body></html>
""" + "<!-- padding -->" * 200


class _FakeRobots:
    """`RobotsRules`の代わり。施設番号で禁止を表現する(テストの読みやすさ優先)。"""

    def __init__(self, disallowed: set[int]) -> None:
        self.disallowed = disallowed


class _FakeClient:
    """`RakutenTravelClient`のうち、取り込みが使う口だけを持つ。"""

    def __init__(
        self,
        entries: list[AreaListEntry] | None = None,
        *,
        disallowed: set[int] | None = None,
        top: dict[int, str] | None = None,
        detail: dict[int, str] | None = None,
        raise_for: dict[int, Exception] | None = None,
    ) -> None:
        self._entries = entries or []
        self._robots = _FakeRobots(disallowed or set())
        self._top = top or {}
        self._detail = detail or {}
        self._raise_for = raise_for or {}
        self.fetched: list[int] = []

    def load_robots(self, host: str = "travel.rakuten.co.jp") -> _FakeRobots:
        return self._robots

    def is_url_allowed(self, url: str) -> bool:
        import re as _re

        m = _re.search(r"/HOTEL/(\d+)/", url)
        return not (m and int(m.group(1)) in self._robots.disallowed)

    def iter_area_list(self, prefecture: str, *, max_pages=None):  # noqa: ANN001, ANN202
        yield from self._entries

    def fetch_top_page(self, hotel_no: int) -> str:
        if hotel_no in self._raise_for:
            raise self._raise_for[hotel_no]
        self.fetched.append(hotel_no)
        return self._top.get(hotel_no, "")

    def fetch_detail_page(self, hotel_no: int) -> str:
        return self._detail.get(hotel_no, "")


def _entry(hotel_no: int, name: str = "テスト旅館") -> AreaListEntry:
    return AreaListEntry(hotel_no=hotel_no, name=name, prefecture="鳥取県", city="鳥取市")


class TestImportPrefectureShallow:
    def test_一覧から施設を組み立てる(self) -> None:
        client = _FakeClient([_entry(1), _entry(2)])
        result = import_prefecture_shallow("鳥取県", client=client)
        assert result.listed_count == 2
        assert [f.hotel_no for f in result.facilities] == [1, 2]

    def test_robotsで禁止された施設は取り込まない(self) -> None:
        # 取り込みバッチの入口でも除外していること（client層の禁止だけに頼らない）。
        client = _FakeClient([_entry(1), _entry(2), _entry(3)], disallowed={2})
        result = import_prefecture_shallow("鳥取県", client=client)
        assert [f.hotel_no for f in result.facilities] == [1, 3]
        assert result.skipped_by_robots == 1
        assert result.listed_count == 2

    def test_この時点でカテゴリを推定する(self) -> None:
        client = _FakeClient([_entry(1, "ペンションひまわり")])
        result = import_prefecture_shallow("鳥取県", client=client)
        assert result.facilities[0].category is FacilityCategory.PENSION


class TestEnrichFacility:
    def test_詳細を埋める(self) -> None:
        facility = Facility(hotel_no=999, name="旧名", prefecture="鳥取県")
        client = _FakeClient(top={999: TOP_HTML}, detail={999: DETAIL_HTML})
        enriched, warning = enrich_facility(facility, client=client)
        assert warning is None
        assert enriched.name == "テスト旅館"
        assert enriched.room_count == 42
        assert enriched.review_count == 120
        assert enriched.photo_count == 48
        assert enriched.has_onsen is True
        assert enriched.custom_page_status is CustomPageStatus.PUBLISHED
        assert enriched.custom_page_count == 1

    def test_ページが取れなければ掲載終了として返す(self) -> None:
        facility = Facility(hotel_no=999, name="消えた宿")
        client = _FakeClient(top={999: ""})
        enriched, warning = enrich_facility(facility, client=client)
        assert enriched.is_listed is False
        assert warning is not None

    def test_客室数が読めなければ警告を返す(self) -> None:
        facility = Facility(hotel_no=999, name="テスト")
        client = _FakeClient(top={999: TOP_HTML}, detail={999: ""})
        enriched, warning = enrich_facility(facility, client=client)
        assert enriched.room_count is None
        assert warning is not None and "総部屋数" in warning


class TestEnrichFacilities:
    def _facility(self, hotel_no: int) -> Facility:
        return Facility(hotel_no=hotel_no, name="テスト", prefecture="鳥取県")

    def test_成功と失敗を分けて数える(self) -> None:
        client = _FakeClient(
            top={1: TOP_HTML, 2: ""},
            detail={1: DETAIL_HTML},
            raise_for={3: RakutenFetchError("接続できない")},
        )
        result = enrich_facilities(
            [self._facility(1), self._facility(2), self._facility(3)],
            client=client,
            max_workers=1,
        )
        assert result.deep_fetched_count == 1
        assert result.unlisted_count == 1
        assert result.failed_count == 1
        assert result.failures[0][0] == 3

    def test_robots禁止は失敗ではなく除外として数える(self) -> None:
        client = _FakeClient(raise_for={1: RobotsDisallowedError("禁止")})
        result = enrich_facilities([self._facility(1)], client=client, max_workers=1)
        assert result.skipped_by_robots == 1
        assert result.failed_count == 0
        assert result.facilities == []


class TestIsHealthy:
    def test_詳細を取っていなければ判定しない(self) -> None:
        assert ImportResult(prefecture="鳥取県").is_healthy is True

    def test_客室数が読めない割合が高ければ不健全(self) -> None:
        result = ImportResult(prefecture="鳥取県", deep_fetched_count=10, missing_room_count=4)
        assert result.is_healthy is False
        assert "客室数" in (result.health_warning or "")

    def test_全部が掲載終了になったら不健全(self) -> None:
        # パーサが壊れて施設名が読めなくなると、生きている施設が丸ごと
        # 「掲載終了」と判定される(shirokuma-secレビュー指摘、2026-09-11)。
        result = ImportResult(prefecture="鳥取県", deep_fetched_count=0, unlisted_count=30)
        assert result.is_healthy is False
        assert result.health_warning is not None

    def test_掲載終了が半数を超えたら不健全(self) -> None:
        result = ImportResult(prefecture="鳥取県", deep_fetched_count=4, unlisted_count=6)
        assert result.is_healthy is False

    def test_通常の欠番なら健全(self) -> None:
        result = ImportResult(
            prefecture="鳥取県", deep_fetched_count=38, unlisted_count=2, missing_room_count=1
        )
        assert result.is_healthy is True
        assert result.health_warning is None
