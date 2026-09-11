"""楽天トラベルの公開ページから施設を取り込むユースケース(2026-09-11)。

取り込みは**2段階**に分けてある。全国43,753施設の詳細を毎回取りに行くのは現実的でなく、
楽天側への負荷としても過大なため。

  第1段階(浅い取り込み)  都道府県のエリア一覧を30件/ページで辿る
                          → 施設名・クチコミ点数・住所・最安料金
                          → 1都道府県あたり数十リクエストで終わる

  第2段階(深い取り込み)  第1段階で絞り込んだ施設だけ、施設ページ2本を取りに行く
                          → 客室数・館内設備・カスタマイズページ有無・温泉
                          → 1施設あたり2リクエスト

「取り込めた件数」だけでなく**失敗件数と主要項目が欠けた件数**も返す。
`rc=0`(例外なく終わったこと)を成功と読み替えないため(AGENTS.md「定期実行はrc=0を信用しない」)。
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace

from src.facility_list.domain.models import (
    CheckInMachineSource,
    CheckInMachineStatus,
    CustomPageStatus,
    Facility,
)
from src.facility_list.domain.services import detect_check_in_machine, estimate_category
from src.facility_list.infrastructure.rakuten_client import (
    DEFAULT_MAX_WORKERS,
    RakutenFetchError,
    RakutenTravelClient,
    RobotsDisallowedError,
)
from src.facility_list.infrastructure.rakuten_page_parser import (
    AreaListEntry,
    parse_detail_page,
    parse_top_page,
)

logger = logging.getLogger(__name__)


@dataclass
class ImportResult:
    """取り込み1回分の結果。**成功件数だけを見て正常と判断しないこと。**"""

    prefecture: str
    listed_count: int = 0  # 一覧から見つけた施設数
    deep_fetched_count: int = 0  # 施設ページまで取れた数
    unlisted_count: int = 0  # 掲載終了(404)だった数
    failed_count: int = 0  # 取得に失敗した数
    skipped_by_robots: int = 0  # robots.txtで禁止されていて触らなかった数
    missing_room_count: int = 0  # 客室数が読めなかった数(楽天側HTML変更の検知用)
    facilities: list[Facility] = field(default_factory=list)
    parse_warnings: dict[int, str] = field(default_factory=dict)
    failures: list[tuple[int, str]] = field(default_factory=list)

    @property
    def is_healthy(self) -> bool:
        """深い取り込みの結果が信用できるか。

        2つの壊れ方を見る。

        ① 施設ページまで取れた施設のうち、客室数が読めなかったものが3割を超えた
           → パーサが静かに壊れている疑い
        ② 詳細を取りに行ったのに、成功が0件で掲載終了ばかりになった
           → `parse_top_page()`が広範囲に失敗している疑い

        **②を「健全」に倒さないことが要点。** 楽天側のHTML変更で施設名が読めなく
        なると、生きている施設が丸ごと「掲載終了」と判定され、`mark_unlisted()`で
        営業リストから静かに消える(shirokuma-secレビュー指摘、2026-09-11)。
        ProjectMirror全消失インシデントと同じ壊れ方なので、ここは疑う側に倒す。
        """
        attempted = self.deep_fetched_count + self.unlisted_count
        if attempted == 0:
            # そもそも詳細を取りに行っていない(浅い取り込みだけ)なら判定しない。
            return True
        if self.deep_fetched_count == 0:
            # 取りに行った全てが「掲載終了」。1件残らず終了しているのは考えにくい。
            return False
        if self.unlisted_count / attempted > 0.5:
            # 半数以上が掲載終了。通常の欠番の割合を明らかに超えている。
            return False
        return self.missing_room_count / self.deep_fetched_count < 0.3

    @property
    def health_warning(self) -> str | None:
        """健全でない場合に、何がおかしいかを日本語で返す。"""
        if self.is_healthy:
            return None
        attempted = self.deep_fetched_count + self.unlisted_count
        if self.deep_fetched_count == 0:
            return f"詳細を取りに行った{attempted}件が全て掲載終了と判定された"
        if self.unlisted_count / attempted > 0.5:
            return f"掲載終了の判定が多すぎる({self.unlisted_count}/{attempted}件)"
        return (
            f"客室数を読めなかった施設が多い"
            f"({self.missing_room_count}/{self.deep_fetched_count}件)"
        )


def _entry_to_facility(entry: AreaListEntry) -> Facility:
    """一覧から取れた情報だけで施設を組み立てる(客室数・設備はまだ不明)。"""
    facility = Facility(
        hotel_no=entry.hotel_no,
        name=entry.name,
        address=entry.address,
        postal_code=entry.postal_code,
        prefecture=entry.prefecture,
        city=entry.city,
        review_average=entry.review_average,
        min_charge=entry.min_charge,
    )
    category, confidence = estimate_category(facility)
    return replace_category(facility, category, confidence)


def replace_category(facility: Facility, category, confidence) -> Facility:
    """`Facility`は不変なので、カテゴリだけ差し替えた複製を返す。"""
    return replace(facility, category=category, category_confidence=confidence)


def import_prefecture_shallow(
    prefecture: str, *, client: RakutenTravelClient, max_pages: int | None = None
) -> ImportResult:
    """第1段階。エリア一覧だけを辿って施設の骨格を作る。"""
    result = ImportResult(prefecture=prefecture)

    for entry in client.iter_area_list(prefecture, max_pages=max_pages):
        facility = _entry_to_facility(entry)
        # 施設ページを取りに行く前に、robots.txt で禁止されていないか見ておく
        # (取得の直前でも`RakutenTravelClient`が見るが、対象から先に外しておけば
        #  無駄なリクエストが1本も出ない)。
        if not client.is_url_allowed(facility.page_url):
            result.skipped_by_robots += 1
            continue
        result.facilities.append(facility)
        result.listed_count += 1

    logger.info(
        "[%s] 一覧から%d件を取り込んだ(robotsで除外:%d件)",
        prefecture,
        result.listed_count,
        result.skipped_by_robots,
    )
    return result


def enrich_facility(facility: Facility, *, client: RakutenTravelClient) -> tuple[Facility, str | None]:
    """第2段階。1施設の施設ページ2本を取りに行き、詳細を埋める。

    戻り値の2つ目は、読み取れなかった主要項目についての警告(無ければNone)。
    """
    top_html = client.fetch_top_page(facility.hotel_no)
    top = parse_top_page(top_html, facility.hotel_no)
    if not top.is_available:
        return replace(facility, is_listed=False), "施設ページが取得できない(掲載終了の可能性)"

    detail_html = client.fetch_detail_page(facility.hotel_no)
    detail = parse_detail_page(detail_html)

    custom_status = (
        CustomPageStatus.PUBLISHED if top.custom_page_ids else CustomPageStatus.NOT_PUBLISHED
    )

    has_onsen = top.has_onsen_page or any("温泉" in b for b in detail.bath_types)

    finding = detect_check_in_machine(
        " ".join(detail.facilities),
        " ".join(detail.room_facilities),
        detail.check_in,
    )

    enriched = replace(
        facility,
        name=top.name or facility.name,
        review_average=top.review_average or facility.review_average,
        review_count=top.review_count,
        photo_count=top.photo_count,
        postal_code=detail.postal_code or facility.postal_code,
        prefecture=detail.prefecture or facility.prefecture,
        city=detail.city or facility.city,
        address=detail.address or facility.address,
        room_count=detail.room_count,
        facilities=detail.facilities,
        room_facilities=detail.room_facilities,
        has_onsen=has_onsen,
        custom_page_status=custom_status,
        custom_page_count=len(top.custom_page_ids),
        check_in_machine=finding.status,
        check_in_machine_source=(
            CheckInMachineSource.FACILITY_PAGE
            if finding.status is CheckInMachineStatus.YES
            else CheckInMachineSource.NONE
        ),
    )
    category, confidence = estimate_category(enriched)
    enriched = replace(enriched, category=category, category_confidence=confidence)

    warning = None if detail.room_count is not None else "総部屋数が読み取れなかった"
    return enriched, warning


def enrich_facilities(
    facilities: list[Facility],
    *,
    client: RakutenTravelClient,
    max_workers: int = DEFAULT_MAX_WORKERS,
    result: ImportResult | None = None,
) -> ImportResult:
    """第2段階をまとめて実行する。

    同時実行数は絞る(既定3)。`RakutenTravelClient`が持つ間隔制御がスレッド間で
    直列化するため、ここを増やしても取得速度は間隔設定で頭打ちになる。
    """
    outcome = result or ImportResult(prefecture=facilities[0].prefecture if facilities else "")

    def work(facility: Facility) -> tuple[Facility, str | None, str | None]:
        try:
            enriched, warning = enrich_facility(facility, client=client)
            return enriched, warning, None
        except RobotsDisallowedError:
            return facility, None, "robots"
        except RakutenFetchError as exc:
            return facility, None, str(exc)

    enriched_list: list[Facility] = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for enriched, warning, error in executor.map(work, facilities):
            if error == "robots":
                outcome.skipped_by_robots += 1
                continue
            if error:
                outcome.failed_count += 1
                outcome.failures.append((enriched.hotel_no, error))
                continue
            if not enriched.is_listed:
                outcome.unlisted_count += 1
            else:
                outcome.deep_fetched_count += 1
                if enriched.room_count is None:
                    outcome.missing_room_count += 1
            if warning:
                outcome.parse_warnings[enriched.hotel_no] = warning
            enriched_list.append(enriched)

    outcome.facilities = enriched_list
    logger.info(
        "詳細取得: 成功%d件 / 掲載終了%d件 / 失敗%d件 / 客室数が読めなかった%d件",
        outcome.deep_fetched_count,
        outcome.unlisted_count,
        outcome.failed_count,
        outcome.missing_room_count,
    )
    if not outcome.is_healthy:
        logger.warning("%s。楽天側のHTML変更を疑うこと", outcome.health_warning)
    return outcome
