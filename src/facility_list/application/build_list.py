"""抽出条件からリストを1本作るユースケース(2026-09-11)。

    DBから取る ──▶ 条件で絞る ──▶ CRMと突合 ──▶ CRM条件で絞る ──▶ 提案商材を付ける

楽天への取得はここでは行わない(取り込みは`import_facilities.py`の担当)。画面から
呼ばれる経路では既に溜めてあるデータを絞るだけなので、待たせずに結果が出る。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from src.facility_list.domain.models import (
    CrmFilter,
    CrmMatch,
    CrmMatchState,
    Facility,
    ListCriteria,
)
from src.facility_list.domain.services import matches_criteria, suggest_products
from src.facility_list.infrastructure import db as facility_db
from src.facility_list.infrastructure.crm_matcher import CrmMatcher

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ListRow:
    """リスト1行。画面・CSV・スプレッドシートで共通に使う。"""

    facility: Facility
    crm: CrmMatch
    products: tuple[str, ...]
    product_reasons: tuple[str, ...]


@dataclass
class ListResult:
    rows: list[ListRow] = field(default_factory=list)
    total_before_crm: int = 0  # CRM突合の前に条件を満たした件数

    @property
    def total(self) -> int:
        return len(self.rows)

    @property
    def matched_count(self) -> int:
        return sum(1 for r in self.rows if r.crm.state is CrmMatchState.MATCHED)

    @property
    def new_count(self) -> int:
        return sum(1 for r in self.rows if r.crm.state is CrmMatchState.NOT_FOUND)

    @property
    def ambiguous_count(self) -> int:
        return sum(1 for r in self.rows if r.crm.state is CrmMatchState.AMBIGUOUS)


def count_candidates(
    criteria: ListCriteria, *, facilities: list[Facility] | None = None
) -> int:
    """CRMを見ない条件だけで数えた件数を返す。

    書き出し前の上限チェックに使う。`build_list()`はCRM条件を指定されたのに
    `matcher`が無ければ止まる作りなので、件数を数えるためだけにそれを呼ぶと
    「未取引のみ」を選んだ瞬間に落ちる(shirokuma-secレビューのBLOCKER、2026-09-11)。
    数えるだけならCRMは要らないので、ここで分けている。

    **CRM条件で絞る前の件数**なので、実際の書き出し件数はこれ以下になる。
    """
    source = (
        facilities
        if facilities is not None
        else facility_db.fetch_facilities(
            prefectures=list(criteria.prefectures) or None, only_listed=True
        )
    )
    return sum(1 for f in source if matches_criteria(f, criteria, crm_match=None))


def build_list(
    criteria: ListCriteria,
    *,
    matcher: CrmMatcher | None = None,
    facilities: list[Facility] | None = None,
) -> ListResult:
    """抽出条件からリストを作る。

    `facilities`を渡すとDBを読まずにそれを母集団として使う(テスト用)。
    `matcher`を渡さなければCRM突合を行わない(CRM由来の条件は効かない)。
    """
    if matcher is None and (
        criteria.crm_filter is not CrmFilter.ANY or criteria.exclude_proposed_services
    ):
        # 突合できないのにCRM条件を指定されたら、黙って無視せず止める。
        # 「未取引だけ」のつもりで全件が出てくる方が危ない。
        raise ValueError("CRM由来の条件を使うにはCrmMatcherが必要(NOTION_API_KEYを設定すること)")

    source = (
        facilities
        if facilities is not None
        else facility_db.fetch_facilities(
            prefectures=list(criteria.prefectures) or None, only_listed=True
        )
    )

    # ① CRMを見ない条件で先に絞る。CRM突合はNotionを読むので、対象を減らしてから行う。
    primary = [f for f in source if matches_criteria(f, criteria, crm_match=None)]
    result = ListResult(total_before_crm=len(primary))
    logger.info("一次抽出: %d件 / 母集団%d件", len(primary), len(source))

    # 担当者連絡先を付けるため、matcherがあるときは常に突合する。
    matches: dict[int, CrmMatch] = {}
    if matcher is not None:
        matches = matcher.match_all(primary)

    unchecked = CrmMatch(state=CrmMatchState.NOT_CHECKED)
    for facility in primary:
        crm = matches.get(facility.hotel_no, unchecked)
        if matcher is not None and not matches_criteria(facility, criteria, crm_match=crm):
            continue
        fits = suggest_products(facility)
        result.rows.append(
            ListRow(
                facility=facility,
                crm=crm,
                products=tuple(f.product for f in fits),
                product_reasons=tuple(f"{f.product}: {f.reason}" for f in fits),
            )
        )
        if criteria.limit is not None and len(result.rows) >= criteria.limit:
            break

    logger.info(
        "抽出結果: %d件(既存%d / 新規%d / 要確認%d)",
        result.total,
        result.matched_count,
        result.new_count,
        result.ambiguous_count,
    )
    return result
