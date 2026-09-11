"""リスト作成マシーンのエンドポイント(2026-09-11)。

母集団は`RakutenFacility`(楽天トラベルの公開ページから取り込み済み)。ここでは
**楽天へは一切アクセスしない**ので、条件を変えて何度でも試せる。

    POST /api/facility-list/preview   条件に当たる件数と先頭数十件(CRM突合なし・速い)
    POST /api/facility-list/export    CRMと突合して全項目を返す(Notionを読むので遅い)

プレビューでCRM突合をしないのは、条件を詰めている最中に毎回Notionを読むと待たされる
ためで、代わりに**プレビューのCRM状態は「未突合」として返す**(未取引と混ぜない)。
"""

from __future__ import annotations

import logging
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from src.api.auth import verify_dashboard_api_token
from src.facility_list.application.build_list import build_list, count_candidates
from src.facility_list.domain.models import (
    CheckInMachineStatus,
    CrmFilter,
    CustomPageStatus,
    FacilityCategory,
    ListCriteria,
    ReviewScoreRange,
    RoomCountRange,
)
from src.facility_list.infrastructure import db as facility_db
from src.facility_list.infrastructure.crm_matcher import CrmMatcher
from src.facility_list.infrastructure.exporters import (
    HEADERS,
    PREVIEW_COLUMNS,
    preview_column_indexes,
    row_to_values,
    to_csv,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# 1回のエクスポートで扱う上限。CRM突合はNotionを1件ずつ読むため、Vercelの
# maxDuration(300秒)に収まる範囲に抑える。超えたら条件を絞ってもらう。
MAX_EXPORT_ROWS = 500
# プレビューで返す行数。件数だけ分かれば条件は詰められる。
PREVIEW_ROWS = 30


class ListCriteriaRequest(BaseModel):
    """画面のフォーム1回分。未指定の項目は絞り込まない。"""

    prefectures: list[str] = Field(default_factory=list)
    room_count_min: int = Field(default=1, ge=0)
    room_count_max: int | None = Field(default=None, gt=0)
    review_min: float | None = Field(default=None, ge=0, le=5)
    review_max: float | None = Field(default=None, ge=0, le=5)
    include_unrated: bool = False
    categories: list[str] = Field(default_factory=list)
    custom_page: str | None = None
    # "yes" | "no" | "unknown"。**"unknown"は「調べたが分からなかった」も
    # 「まだ調べていない」も含む**ので、これで絞ったリストを「導入していない施設」
    # として扱わないこと。
    check_in_machine: str | None = None
    has_onsen: bool | None = None
    min_review_count: int | None = Field(default=None, ge=0)
    max_photo_count: int | None = Field(default=None, ge=0)
    crm_filter: str = "any"
    exclude_proposed_services: list[str] = Field(default_factory=list)
    exclude_chains: bool = False
    limit: int | None = Field(default=None, gt=0)

    def to_criteria(self) -> ListCriteria:
        try:
            room_count = RoomCountRange(self.room_count_min, self.room_count_max)
            review = ReviewScoreRange(
                minimum=Decimal(str(self.review_min)) if self.review_min is not None else None,
                maximum=Decimal(str(self.review_max)) if self.review_max is not None else None,
                include_unrated=self.include_unrated,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        try:
            categories = tuple(FacilityCategory(c) for c in self.categories)
            custom_page = CustomPageStatus(self.custom_page) if self.custom_page else None
            check_in_machine = (
                CheckInMachineStatus(self.check_in_machine) if self.check_in_machine else None
            )
            crm_filter = CrmFilter(self.crm_filter)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"未知の指定です: {exc}") from exc

        return ListCriteria(
            prefectures=tuple(self.prefectures),
            room_count=room_count,
            review_score=review,
            categories=categories,
            custom_page=custom_page,
            check_in_machine=check_in_machine,
            has_onsen=self.has_onsen,
            min_review_count=self.min_review_count,
            max_photo_count=self.max_photo_count,
            crm_filter=crm_filter,
            exclude_proposed_services=tuple(self.exclude_proposed_services),
            exclude_chains=self.exclude_chains,
            limit=self.limit,
        )


@router.post("/api/facility-list/preview", dependencies=[Depends(verify_dashboard_api_token)])
def preview_facility_list(request: ListCriteriaRequest) -> dict:
    """条件に当たる件数と先頭数十件を返す。CRMは見ない(速さ優先)。"""
    criteria = request.to_criteria()
    if criteria.crm_filter is not CrmFilter.ANY or criteria.exclude_proposed_services:
        # CRM条件はプレビューでは効かせられない。黙って無視すると件数を誤解させる。
        raise HTTPException(
            status_code=422,
            detail="CRMに関する条件はプレビューでは使えません。書き出しで指定してください",
        )

    result = build_list(criteria)
    # 全列を返すと画面が横に長くなりすぎるので、プレビューに出す列だけ抜いて返す。
    # どの列を出すかはバックエンド(exporters.PREVIEW_COLUMNS)が決める。画面側が
    # 何番目かを数えると、列の並びを変えたときに静かにズレる。
    indexes = preview_column_indexes()
    rows = [
        [row_to_values(row)[i] for i in indexes] for row in result.rows[:PREVIEW_ROWS]
    ]
    return {
        "total": result.total,
        "headers": list(PREVIEW_COLUMNS),
        "all_headers": list(HEADERS),
        "rows": rows,
        "truncated": result.total > PREVIEW_ROWS,
        "crm_checked": False,
    }


class ExportRequest(ListCriteriaRequest):
    created_by: str = Field(default="", max_length=100)
    # 履歴(FacilityListRun)を誰の実行として残すか。ダッシュボード側が
    # ログイン中のユーザーから埋める(クライアントの申告は使わない)。
    user_id: str = Field(default="", max_length=64)


@router.post("/api/facility-list/export", dependencies=[Depends(verify_dashboard_api_token)])
def export_facility_list(request: ExportRequest) -> dict:
    """CRMと突合した完全なリストをCSV文字列で返す。"""
    criteria = request.to_criteria()

    # 件数を先に数える。CRM突合の前に上限を超えていれば、Notionを読まずに断る。
    # **`build_list()`ではなく`count_candidates()`を使う。** 前者はCRM条件を
    # 指定されたのにmatcherが無いと止まる作りなので、「未取引のみ」を選んだだけで
    # 500になっていた(shirokuma-secレビューのBLOCKER、2026-09-11)。
    candidate_count = count_candidates(criteria)
    if candidate_count > MAX_EXPORT_ROWS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"{candidate_count}件は一度に書き出せません(上限{MAX_EXPORT_ROWS}件)。"
                "都道府県や客室数で絞ってください"
            ),
        )

    matcher = CrmMatcher()
    if not matcher.has_notion_access and criteria.exclude_proposed_services:
        # Notionを読めないと「提案済みサービス」が空で返る。黙って通すと
        # 「提案済みを除いたつもりが1件も除かれていない」リストになる
        # (obasan-qualityレビュー指摘、2026-09-11)。
        raise HTTPException(
            status_code=503,
            detail="CRM(Notion)に接続できないため、提案済みサービスでの絞り込みは使えません",
        )

    result = build_list(criteria, matcher=matcher)

    # 「先週と同じ条件でもう1回」が現場で必ず起きるので、条件と件数を残す。
    # 失敗しても書き出しは止めない(db.record_list_run内で握る)。
    if request.user_id:
        facility_db.record_list_run(
            user_id=request.user_id,
            criteria=request.model_dump(exclude={"user_id", "created_by"}),
            total_count=result.total,
            matched_count=result.matched_count,
            new_count=result.new_count,
            ambiguous_count=result.ambiguous_count,
        )

    return {
        "total": result.total,
        "matched_count": result.matched_count,
        "new_count": result.new_count,
        "ambiguous_count": result.ambiguous_count,
        "csv": to_csv(result, created_by=request.created_by),
        "crm_checked": True,
        # Notionを読めていない場合、取引先名までは分かっても提案済みサービスや
        # 連絡先は空になる。画面がそれを伝えられるよう返す。
        "contacts_available": matcher.has_notion_access,
    }
