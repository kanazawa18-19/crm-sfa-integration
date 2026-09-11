"""リスト作成マシーンのドメイン層の検証。

Domain層はDBも外部APIも起動せず単体テストできること、がAGENTS.mdのDDD合格判定なので、
このファイルにはモックが1つも出てこない。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.facility_list.domain.models import (
    CategoryConfidence,
    CheckInMachineStatus,
    CrmFilter,
    CrmMatch,
    CrmMatchState,
    CustomPageStatus,
    Facility,
    FacilityCategory,
    ListCriteria,
    ReviewScoreRange,
    RoomCountRange,
)
from src.facility_list.domain.services import (
    detect_check_in_machine,
    estimate_category,
    is_chain_facility,
    matches_criteria,
    suggest_products,
)


class TestRoomCountRange:
    def test_以上未満で判定する(self) -> None:
        r = RoomCountRange(10, 30)
        assert not r.contains(9)
        assert r.contains(10)
        assert r.contains(29)
        assert not r.contains(30)

    def test_上限なしは既定(self) -> None:
        r = RoomCountRange()
        assert r.contains(1)
        assert r.contains(10_000)

    def test_客室数が不明な施設は既定でも含めない(self) -> None:
        # 「不明」を「条件を満たす」に混ぜると、客室数で料金が決まるリピッテの
        # 提案額が出せない行がリストに混ざる。
        assert not RoomCountRange().contains(None)

    def test_上限が下限以下なら作れない(self) -> None:
        with pytest.raises(ValueError):
            RoomCountRange(30, 30)


class TestReviewScoreRange:
    def test_以上未満で判定する(self) -> None:
        r = ReviewScoreRange(minimum=Decimal("3.0"), maximum=Decimal("4.0"))
        assert not r.contains(Decimal("2.99"))
        assert r.contains(Decimal("3.0"))
        assert not r.contains(Decimal("4.0"))

    def test_クチコミなしは既定で除外し明示すれば含める(self) -> None:
        assert not ReviewScoreRange().contains(None)
        assert ReviewScoreRange(include_unrated=True).contains(None)


class TestEstimateCategory:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("ペンションひまわり", FacilityCategory.PENSION),
            ("民宿かもめ", FacilityCategory.PENSION),
            ("四万温泉　積善館旅館", FacilityCategory.RYOKAN),
            ("貸別荘ログハウス村", FacilityCategory.VILLA),
        ],
    )
    def test_施設名に決め手があれば高い確からしさで決まる(
        self, name: str, expected: FacilityCategory
    ) -> None:
        category, confidence = estimate_category(Facility(hotel_no=1, name=name))
        assert category is expected
        assert confidence is CategoryConfidence.HIGH

    def test_名前がホテルでも確からしさは中どまり(self) -> None:
        # 「皆生温泉 皆生グランドホテル天水」のように名前はホテルでも実態は旅館、
        # という施設が実在する(2026-09-11、鳥取県の実データ)。
        category, confidence = estimate_category(
            Facility(hotel_no=1, name="ハートンホテル西梅田", room_count=471)
        )
        assert category is FacilityCategory.HOTEL
        assert confidence is CategoryConfidence.MEDIUM

    def test_和の設備が揃う小規模な宿は旅館を疑う(self) -> None:
        facility = Facility(
            hotel_no=1,
            name="皆生温泉　グランドホテル天水",
            room_count=60,
            has_onsen=True,
            room_facilities=("浴衣",),
            facilities=("露天風呂",),
        )
        category, confidence = estimate_category(facility)
        assert category is FacilityCategory.RYOKAN
        assert confidence is CategoryConfidence.LOW

    def test_チェーンは名前が和風でもホテル(self) -> None:
        facility = Facility(
            hotel_no=1,
            name="天然温泉　御宿　野乃（ドーミーインＰＲＥＭＩＵＭ）",
            room_count=194,
            has_onsen=True,
            room_facilities=("浴衣",),
        )
        category, _ = estimate_category(facility)
        assert category is FacilityCategory.HOTEL

    def test_手がかりが無ければ不明にする(self) -> None:
        category, confidence = estimate_category(Facility(hotel_no=1, name="さくら"))
        assert category is FacilityCategory.UNKNOWN
        assert confidence is CategoryConfidence.NONE


class TestDetectCheckInMachine:
    def test_記載があればありと根拠を返す(self) -> None:
        finding = detect_check_in_machine("フロントに自動チェックイン機を設置しています")
        assert finding.status is CheckInMachineStatus.YES
        assert "自動チェックイン機" in (finding.evidence or "")

    def test_記載が無くてもなしとは言わない(self) -> None:
        # 楽天のページに書かれていないことは「導入していない」ことの証明にならない。
        finding = detect_check_in_machine("通常のフロント対応です")
        assert finding.status is CheckInMachineStatus.UNKNOWN
        assert finding.evidence is None


class TestMatchesCriteria:
    def _facility(self, **overrides) -> Facility:
        base = {
            "hotel_no": 1,
            "name": "テスト旅館",
            "prefecture": "鳥取県",
            "room_count": 30,
            "review_average": Decimal("4.0"),
            "review_count": 100,
            "category": FacilityCategory.RYOKAN,
            "custom_page_status": CustomPageStatus.NOT_PUBLISHED,
        }
        base.update(overrides)
        return Facility(**base)

    def test_都道府県で絞る(self) -> None:
        f = self._facility()
        assert matches_criteria(f, ListCriteria(prefectures=("鳥取県",)))
        assert not matches_criteria(f, ListCriteria(prefectures=("島根県",)))

    def test_カスタマイズページ未作成で絞る(self) -> None:
        f = self._facility()
        assert matches_criteria(f, ListCriteria(custom_page=CustomPageStatus.NOT_PUBLISHED))
        assert not matches_criteria(f, ListCriteria(custom_page=CustomPageStatus.PUBLISHED))

    def test_チェーンを除外する(self) -> None:
        f = self._facility(name="アパホテル〈新大阪駅前〉")
        assert not matches_criteria(f, ListCriteria(exclude_chains=True))
        assert matches_criteria(f, ListCriteria(exclude_chains=False))

    def test_写真が取れていない施設は枚数条件に通さない(self) -> None:
        f = self._facility(photo_count=None)
        assert not matches_criteria(f, ListCriteria(max_photo_count=20))

    def test_新規のみでは未突合も候補複数も通さない(self) -> None:
        f = self._facility()
        criteria = ListCriteria(crm_filter=CrmFilter.NEW_ONLY)
        assert matches_criteria(f, criteria, CrmMatch(state=CrmMatchState.NO_NAME_MATCH))
        assert not matches_criteria(f, criteria, CrmMatch(state=CrmMatchState.AMBIGUOUS))
        assert not matches_criteria(f, criteria, CrmMatch(state=CrmMatchState.NOT_CHECKED))
        assert not matches_criteria(f, criteria, CrmMatch(state=CrmMatchState.MATCHED))

    def test_提案済みの商材を除外する(self) -> None:
        f = self._facility()
        criteria = ListCriteria(exclude_proposed_services=("フルスコ",))
        proposed = CrmMatch(state=CrmMatchState.MATCHED, proposed_services=("フルスコ",))
        assert not matches_criteria(f, criteria, proposed)
        assert matches_criteria(f, criteria, CrmMatch(state=CrmMatchState.MATCHED))


class TestSuggestProducts:
    def test_カスタマイズページ未作成なら制作を提案する(self) -> None:
        f = Facility(
            hotel_no=1, name="テスト", custom_page_status=CustomPageStatus.NOT_PUBLISHED
        )
        products = [p.product for p in suggest_products(f)]
        assert "WEB制作（楽天CP）" in products

    def test_客室数からリピッテの料金帯を出す(self) -> None:
        fits = suggest_products(Facility(hotel_no=1, name="テスト", room_count=20))
        repitte = next(p for p in fits if p.product == "リピッテホテル")
        assert "9,800" in repitte.reason

    def test_点数が低く母数もあればクチコミ商材を提案する(self) -> None:
        f = Facility(
            hotel_no=1, name="テスト", review_average=Decimal("3.5"), review_count=200
        )
        products = [p.product for p in suggest_products(f)]
        assert "フルスコ" in products
        assert "ホテルラボ レビュー" in products

    def test_母数が少なければクチコミ商材は提案しない(self) -> None:
        f = Facility(hotel_no=1, name="テスト", review_average=Decimal("3.5"), review_count=3)
        products = [p.product for p in suggest_products(f)]
        assert "フルスコ" not in products


def test_チェーン判定() -> None:
    assert is_chain_facility(Facility(hotel_no=1, name="東横イン鳥取駅前"))
    assert not is_chain_facility(Facility(hotel_no=2, name="皆生温泉　華水亭"))


class TestMatchesCriteriaRemaining:
    """QAレビュー(2026-09-11)で未カバーと指摘された分岐と境界値。"""

    def _facility(self, **overrides) -> Facility:
        base = {
            "hotel_no": 1,
            "name": "テスト旅館",
            "prefecture": "鳥取県",
            "room_count": 30,
            "review_average": Decimal("4.0"),
            "review_count": 100,
            "photo_count": 20,
            "category": FacilityCategory.RYOKAN,
        }
        base.update(overrides)
        return Facility(**base)

    def test_施設カテゴリーで絞る(self) -> None:
        f = self._facility()
        assert matches_criteria(f, ListCriteria(categories=(FacilityCategory.RYOKAN,)))
        assert not matches_criteria(f, ListCriteria(categories=(FacilityCategory.HOTEL,)))
        # 複数指定はどれかに当たればよい。
        assert matches_criteria(
            f, ListCriteria(categories=(FacilityCategory.HOTEL, FacilityCategory.RYOKAN))
        )

    def test_温泉の有無で絞る(self) -> None:
        onsen = self._facility(has_onsen=True)
        dry = self._facility(has_onsen=False)
        assert matches_criteria(onsen, ListCriteria(has_onsen=True))
        assert not matches_criteria(dry, ListCriteria(has_onsen=True))
        assert matches_criteria(dry, ListCriteria(has_onsen=False))

    def test_チェックイン機の状態で絞る(self) -> None:
        yes = self._facility(check_in_machine=CheckInMachineStatus.YES)
        unknown = self._facility()
        assert matches_criteria(yes, ListCriteria(check_in_machine=CheckInMachineStatus.YES))
        assert not matches_criteria(unknown, ListCriteria(check_in_machine=CheckInMachineStatus.YES))
        assert matches_criteria(unknown, ListCriteria(check_in_machine=CheckInMachineStatus.UNKNOWN))

    def test_クチコミ件数の下限と不明の扱い(self) -> None:
        assert matches_criteria(self._facility(review_count=50), ListCriteria(min_review_count=50))
        assert not matches_criteria(
            self._facility(review_count=49), ListCriteria(min_review_count=50)
        )
        # 件数が分かっていない施設は「50件以上」を満たしたことにしない。
        assert not matches_criteria(
            self._facility(review_count=None), ListCriteria(min_review_count=50)
        )

    def test_写真枚数は未満で判定する(self) -> None:
        assert not matches_criteria(self._facility(photo_count=20), ListCriteria(max_photo_count=20))
        assert matches_criteria(self._facility(photo_count=19), ListCriteria(max_photo_count=20))

    def test_既存取引先のみでは確定したものだけ通す(self) -> None:
        f = self._facility()
        criteria = ListCriteria(crm_filter=CrmFilter.EXISTING_ONLY)
        assert matches_criteria(f, criteria, CrmMatch(state=CrmMatchState.MATCHED))
        assert not matches_criteria(f, criteria, CrmMatch(state=CrmMatchState.AMBIGUOUS))
        assert not matches_criteria(f, criteria, CrmMatch(state=CrmMatchState.NOT_CHECKED))
        assert not matches_criteria(f, criteria, CrmMatch(state=CrmMatchState.NO_NAME_MATCH))

    def test_取引中サービスも提案済み除外の対象にする(self) -> None:
        f = self._facility()
        criteria = ListCriteria(exclude_proposed_services=("フルスコ",))
        contracted = CrmMatch(state=CrmMatchState.MATCHED, contracted_services=("フルスコ",))
        assert not matches_criteria(f, criteria, contracted)


class TestSuggestProductsBoundaries:
    """商材提案の境界値（QAレビュー指摘、2026-09-11）。"""

    def test_リピッテの料金帯の境目(self) -> None:
        def price(rooms: int) -> str:
            fits = suggest_products(Facility(hotel_no=1, name="テスト", room_count=rooms))
            return next(p.reason for p in fits if p.product == "リピッテホテル")

        assert "6,800" in price(9)
        assert "9,800" in price(10)
        assert "9,800" in price(29)
        assert "13,800" in price(30)
        assert "13,800" in price(49)
        assert "要問合せ" in price(50)

    def test_RMは50室ちょうどから提案する(self) -> None:
        def products(rooms: int) -> list[str]:
            return [p.product for p in suggest_products(Facility(hotel_no=1, name="テスト", room_count=rooms))]

        assert "ホテルラボRM" not in products(49)
        assert "ホテルラボRM" in products(50)

    def test_クチコミ商材は件数50点数40の境目で切り替わる(self) -> None:
        def products(score: str, count: int) -> list[str]:
            facility = Facility(
                hotel_no=1, name="テスト", review_average=Decimal(score), review_count=count
            )
            return [p.product for p in suggest_products(facility)]

        assert "フルスコ" in products("3.99", 50)
        assert "フルスコ" not in products("4.0", 50)
        assert "フルスコ" not in products("3.99", 49)

    def test_写真は20枚未満で撮影提案(self) -> None:
        def products(photos: int) -> list[str]:
            return [
                p.product
                for p in suggest_products(Facility(hotel_no=1, name="テスト", photo_count=photos))
            ]

        assert "ホテルラボ（OTA写真撮影）" in products(19)
        assert "ホテルラボ（OTA写真撮影）" not in products(20)
