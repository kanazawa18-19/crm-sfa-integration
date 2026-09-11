"""リスト生成と出力の検証。DBもNotionも触らず、施設を直接渡して確かめる。"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from src.facility_list.application.build_list import build_list
from src.facility_list.domain.models import (
    CrmContact,
    CrmFilter,
    CrmMatch,
    CrmMatchState,
    CustomPageStatus,
    Facility,
    FacilityCategory,
    ListCriteria,
    RoomCountRange,
)
from src.facility_list.infrastructure.exporters import HEADERS, build_issue_text, to_csv, to_rows


def _facility(hotel_no: int, name: str, **overrides) -> Facility:
    base = {
        "hotel_no": hotel_no,
        "name": name,
        "prefecture": "鳥取県",
        "city": "米子市",
        "room_count": 50,
        "review_average": Decimal("4.0"),
        "review_count": 100,
        "category": FacilityCategory.RYOKAN,
    }
    base.update(overrides)
    return Facility(**base)


class _StubMatcher:
    """CRM突合の代わり。Notionにもローカルのインデックスにも触らない。"""

    def __init__(self, matches: dict[int, CrmMatch]) -> None:
        self._matches = matches

    def match_all(self, facilities):  # noqa: ANN001
        return {f.hotel_no: self._matches.get(f.hotel_no, CrmMatch(CrmMatchState.NOT_FOUND))
                for f in facilities}


class TestBuildList:
    def test_条件に当たる施設だけ返す(self) -> None:
        facilities = [
            _facility(1, "華水亭", room_count=50),
            _facility(2, "小さな宿", room_count=5),
        ]
        result = build_list(
            ListCriteria(room_count=RoomCountRange(10, None)), facilities=facilities
        )
        assert [row.facility.hotel_no for row in result.rows] == [1]
        assert result.total_before_crm == 1

    def test_突合していないときは未取引と言わない(self) -> None:
        result = build_list(ListCriteria(), facilities=[_facility(1, "華水亭")])
        assert result.rows[0].crm.state is CrmMatchState.NOT_CHECKED
        assert result.new_count == 0

    def test_CRM条件を使うのにmatcherが無ければ拒否する(self) -> None:
        # 黙って無視すると「未取引だけ」のつもりで全件が出てくる。
        with pytest.raises(ValueError):
            build_list(
                ListCriteria(crm_filter=CrmFilter.NEW_ONLY), facilities=[_facility(1, "華水亭")]
            )

    def test_突合した結果で絞り込む(self) -> None:
        facilities = [_facility(1, "華水亭"), _facility(2, "望湖楼")]
        matcher = _StubMatcher(
            {
                1: CrmMatch(state=CrmMatchState.MATCHED, client_name="株式会社華水亭"),
                2: CrmMatch(state=CrmMatchState.NOT_FOUND),
            }
        )
        result = build_list(
            ListCriteria(crm_filter=CrmFilter.NEW_ONLY), matcher=matcher, facilities=facilities
        )
        assert [row.facility.hotel_no for row in result.rows] == [2]
        assert result.new_count == 1
        assert result.matched_count == 0

    def test_上限で打ち切る(self) -> None:
        facilities = [_facility(i, f"宿{i}") for i in range(1, 11)]
        result = build_list(ListCriteria(limit=3), facilities=facilities)
        assert result.total == 3


class TestExporters:
    def test_指定された列の並びで出す(self) -> None:
        assert HEADERS[:9] == (
            "施設名",
            "コールステータス",
            "担当",
            "電話番号",
            "メモ",
            "姿勢",
            "顕在課題",
            "現状の取引",
            "URL",
        )

    def test_営業が埋める欄は空のまま出す(self) -> None:
        result = build_list(ListCriteria(), facilities=[_facility(1, "華水亭")])
        rows = to_rows(result, created_by="金沢", created_at=datetime(2026, 9, 11, 21, 0))
        header, row = rows[0], rows[1]
        for column in ("コールステータス", "メモ", "姿勢", "日付"):
            assert row[header.index(column)] == ""

    def test_作成者と作成日時を入れる(self) -> None:
        result = build_list(ListCriteria(), facilities=[_facility(1, "華水亭")])
        rows = to_rows(result, created_by="金沢", created_at=datetime(2026, 9, 11, 21, 0))
        header, row = rows[0], rows[1]
        assert row[header.index("作成者")] == "金沢"
        assert row[header.index("作成日時")] == "2026-09-11 21:00"

    def test_顕在課題は事実だけを並べる(self) -> None:
        facility = _facility(
            1,
            "望湖楼",
            review_average=Decimal("3.9"),
            review_count=1579,
            photo_count=12,
            custom_page_status=CustomPageStatus.NOT_PUBLISHED,
        )
        result = build_list(ListCriteria(), facilities=[facility])
        text = build_issue_text(result.rows[0])
        assert "楽天カスタマイズページが未作成" in text
        assert "3.9" in text
        assert "12枚" in text

    def test_課題が無ければ空にする(self) -> None:
        facility = _facility(
            1,
            "優等生の宿",
            review_average=Decimal("4.8"),
            review_count=500,
            photo_count=120,
            custom_page_status=CustomPageStatus.PUBLISHED,
        )
        result = build_list(ListCriteria(), facilities=[facility])
        assert build_issue_text(result.rows[0]) == ""

    def test_連絡先を1件目だけ載せる(self) -> None:
        matcher = _StubMatcher(
            {
                1: CrmMatch(
                    state=CrmMatchState.MATCHED,
                    client_name="株式会社華水亭",
                    client_fax="0859-00-0000",
                    contacts=(
                        CrmContact(
                            contact_page_id="a" * 32,
                            name="山田太郎",
                            email="yamada@example.com",
                            title="支配人",
                        ),
                    ),
                )
            }
        )
        result = build_list(ListCriteria(), matcher=matcher, facilities=[_facility(1, "華水亭")])
        rows = to_rows(result)
        header, row = rows[0], rows[1]
        assert row[header.index("メール")] == "yamada@example.com"
        assert row[header.index("担当者名")] == "山田太郎"
        assert row[header.index("役職")] == "支配人"
        assert row[header.index("FAX番号")] == "0859-00-0000"

    def test_CSVはExcelで開けるようBOMを付ける(self) -> None:
        result = build_list(ListCriteria(), facilities=[_facility(1, "華水亭")])
        csv_text = to_csv(result)
        assert csv_text.startswith("﻿")
        assert "施設名" in csv_text.splitlines()[0]

    def test_不明を_なし_に書き換えない(self) -> None:
        result = build_list(ListCriteria(), facilities=[_facility(1, "華水亭")])
        rows = to_rows(result)
        header, row = rows[0], rows[1]
        assert row[header.index("チェックイン機")] == "不明"


class TestLabelCoverage:
    """enumを増やしたときにラベル辞書の更新漏れで列が空欄になるのを防ぐ。"""

    def test_全てのenum値にラベルがある(self) -> None:
        from src.facility_list.domain.models import (
            CategoryConfidence,
            CheckInMachineStatus,
            CustomPageStatus,
            FacilityCategory,
        )
        from src.facility_list.infrastructure import exporters

        pairs = [
            (FacilityCategory, exporters._CATEGORY_LABELS),
            (CategoryConfidence, exporters._CONFIDENCE_LABELS),
            (CustomPageStatus, exporters._CUSTOM_PAGE_LABELS),
            (CheckInMachineStatus, exporters._CHECK_IN_LABELS),
            (CrmMatchState, exporters._CRM_STATE_LABELS),
        ]
        for enum_type, labels in pairs:
            keys = set(labels)
            missing = [
                member
                for member in enum_type
                if member not in keys and member.value not in keys
            ]
            assert not missing, f"{enum_type.__name__}のラベルが無い: {missing}"

    def test_プレビュー列は全てCSVの列に存在する(self) -> None:
        from src.facility_list.infrastructure.exporters import (
            HEADERS,
            PREVIEW_COLUMNS,
            preview_column_indexes,
        )

        assert set(PREVIEW_COLUMNS) <= set(HEADERS)
        assert len(preview_column_indexes()) == len(PREVIEW_COLUMNS)
