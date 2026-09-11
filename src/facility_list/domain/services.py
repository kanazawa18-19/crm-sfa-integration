"""施設カテゴリの推定・抽出条件の判定・商材適合の判定(2026-09-11)。

いずれも入力から出力が決まる純粋関数で、I/Oを一切持たない。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

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
)

# ---------------------------------------------------------------------------
# カテゴリ推定
# ---------------------------------------------------------------------------

# 施設名に含まれていれば、それだけでカテゴリを決めてよい語(確からしさHIGH)。
# 上から順に見て最初に当たったものを採用するため、より限定的な語を先に置く。
_NAME_SIGNALS: tuple[tuple[FacilityCategory, tuple[str, ...]], ...] = (
    (
        FacilityCategory.VILLA,
        ("貸別荘", "一棟貸", "コテージ", "グランピング", "ヴィラ", "ＶＩＬＬＡ", "VILLA"),
    ),
    (
        FacilityCategory.PENSION,
        (
            "ペンション",
            "民宿",
            "ゲストハウス",
            "ホステル",
            "ＨＯＳＴＥＬ",
            "HOSTEL",
            "ロッジ",
            "ユースホステル",
            "ドミトリー",
        ),
    ),
    (
        FacilityCategory.RYOKAN,
        ("旅館", "旅亭", "湯宿", "温泉宿", "料亭", "ryokan", "ＲＹＯＫＡＮ"),
    ),
)

# 「ホテル」は決め手にならない。「皆生温泉 皆生グランドホテル天水」のように、名前は
# ホテルでも実態は和風旅館という施設が実在する(2026-09-11、鳥取県の実データで確認)。
# そのため`HIGH`ではなく`MEDIUM`止まりにし、和の設備が揃っていれば旅館を疑う。
_HOTEL_NAME_KEYWORDS = ("ホテル", "ＨＯＴＥＬ", "HOTEL", "リゾート", "ＩＮＮ", "INN")

# 旅館らしさの強いシグナル。浴衣と泉質が揃うのは温泉旅館にほぼ限られる。
_STRONG_RYOKAN_FACILITIES = ("浴衣", "露天風呂", "貸切風呂", "囲炉裏", "茶室", "仲居")

# 施設名の末尾がこれらなら旅館・民宿の可能性が高いが、単独では決め手にならない
# (「〜荘」はアパートにも使われる)。設備と合わせてMEDIUM判定に使う。
_WEAK_RYOKAN_SUFFIXES = ("荘", "館", "屋", "亭", "の宿", "庵")

# 旅館らしさを示す設備。浴衣は旅館・温泉宿にほぼ限られ、ビジネスホテルには稀。
_RYOKAN_FACILITY_SIGNALS = ("浴衣", "露天風呂", "貸切風呂", "温泉", "囲炉裏", "茶室")


def estimate_category(facility: Facility) -> tuple[FacilityCategory, CategoryConfidence]:
    """施設名と設備からカテゴリを推定する。

    **楽天トラベルの公開ページにカテゴリ項目は存在しない**ため、ここでの結果は常に推定
    である(2026-09-11実測)。呼び出し側は`CategoryConfidence`を画面に出し、確定情報として
    扱わせないこと。
    """
    name = facility.name or ""
    # 全角英字を含む施設名が多いため、記号・空白を落として素朴に部分一致で見る。
    normalized = re.sub(r"[\s　]+", "", name)

    for category, keywords in _NAME_SIGNALS:
        if any(kw in normalized for kw in keywords):
            return category, CategoryConfidence.HIGH

    onsen_like = facility.has_onsen or any(
        facility.has_facility(kw) for kw in _RYOKAN_FACILITY_SIGNALS
    )
    ryokan_like = any(facility.has_facility(kw) for kw in _STRONG_RYOKAN_FACILITIES)
    weak_suffix = any(normalized.endswith(sfx) for sfx in _WEAK_RYOKAN_SUFFIXES)
    hotel_name = any(kw in normalized for kw in _HOTEL_NAME_KEYWORDS)

    # チェーン系は名前が和風でも運営はホテル(例: ドーミーインの「野乃」)。
    # `is_chain_facility()`で先に確定させる。
    if is_chain_facility(facility):
        return FacilityCategory.HOTEL, CategoryConfidence.MEDIUM

    if hotel_name:
        # 名前はホテルでも、和の設備が揃い規模が小さければ旅館の可能性が高い。
        # 確信は持てないので`LOW`で返し、画面では「推定(弱)」として出させる。
        small = facility.room_count is not None and facility.room_count < 80
        if ryokan_like and onsen_like and small:
            return FacilityCategory.RYOKAN, CategoryConfidence.LOW
        return FacilityCategory.HOTEL, CategoryConfidence.MEDIUM

    if ryokan_like and onsen_like:
        return FacilityCategory.RYOKAN, CategoryConfidence.MEDIUM
    if onsen_like and weak_suffix:
        return FacilityCategory.RYOKAN, CategoryConfidence.MEDIUM
    if onsen_like or weak_suffix:
        return FacilityCategory.RYOKAN, CategoryConfidence.LOW

    return FacilityCategory.UNKNOWN, CategoryConfidence.NONE


# ---------------------------------------------------------------------------
# 抽出条件の判定
# ---------------------------------------------------------------------------

# チェーン本部が一括で判断するため、単館への営業が通りにくい系列。
# `exclude_chains`が真のときに施設名の部分一致で除外する。運用しながら足す前提。
CHAIN_NAME_PATTERNS: tuple[str, ...] = (
    "アパホテル",
    "ＡＰＡ",
    "東横イン",
    "ルートイン",
    "ドーミーイン",
    "スーパーホテル",
    "コンフォートホテル",
    "リッチモンドホテル",
    "ダイワロイネット",
    "サンルート",
    "ヴィアイン",
    "相鉄フレッサイン",
    "東急ＲＥＩ",
    "ワシントンホテル",
    "プリンスホテル",
    "ＪＲイン",
)


def chain_name_of(facility: Facility) -> str | None:
    """施設名から系列名を返す(当たらなければNone)。リストの「チェーン」列に出す。"""
    normalized = re.sub(r"[\s　]+", "", facility.name or "")
    for pattern in CHAIN_NAME_PATTERNS:
        if pattern in normalized:
            return pattern
    return None


def is_chain_facility(facility: Facility) -> bool:
    return chain_name_of(facility) is not None


def matches_criteria(
    facility: Facility, criteria: ListCriteria, crm_match: CrmMatch | None = None
) -> bool:
    """施設が抽出条件を満たすか。条件は全てANDで効く。

    `crm_match`が`None`のときはCRM由来の条件(取引状態・提案済みサービス)を評価しない
    (突合前の一次絞り込みで使う)。
    """
    if criteria.prefectures and facility.prefecture not in criteria.prefectures:
        return False
    if not criteria.room_count.contains(facility.room_count):
        return False
    if not criteria.review_score.contains(facility.review_average):
        return False
    if criteria.categories and facility.category not in criteria.categories:
        return False
    if criteria.custom_page is not None and facility.custom_page_status != criteria.custom_page:
        return False
    if (
        criteria.check_in_machine is not None
        and facility.check_in_machine != criteria.check_in_machine
    ):
        return False
    if criteria.has_onsen is not None and facility.has_onsen != criteria.has_onsen:
        return False
    if criteria.min_review_count is not None:
        if facility.review_count is None or facility.review_count < criteria.min_review_count:
            return False
    if criteria.max_photo_count is not None:
        # 写真枚数が取れていない施設は「少ない」と断定できないので除外する。
        if facility.photo_count is None or facility.photo_count >= criteria.max_photo_count:
            return False
    if criteria.exclude_chains and is_chain_facility(facility):
        return False

    if crm_match is not None:
        if criteria.crm_filter is CrmFilter.NEW_ONLY:
            # 通すのは`NO_NAME_MATCH`（名前照合で当たらなかった）だけ。
            # AMBIGUOUS(候補が複数)とNOT_CHECKED(突合できず)は絶対に通さない。
            # 既存顧客へ新規営業をかける事故が起きるため。
            #
            # **ただし`NO_NAME_MATCH`も「未取引」の証明ではない。** CRMに運営会社名で
            # 載っている既存顧客は施設名では当たらないので、このリストには混ざりうる
            # (他社レビュー指摘、2026-09-12)。住所・電話での二次照合は未実装。
            # CSVの「CRM状態」列は「未取引の可能性（名前照合のみ）」と出している。
            if crm_match.state is not CrmMatchState.NO_NAME_MATCH:
                return False
        elif criteria.crm_filter is CrmFilter.EXISTING_ONLY:
            if crm_match.state is not CrmMatchState.MATCHED:
                return False
        if criteria.exclude_proposed_services:
            already = set(crm_match.proposed_services) | set(crm_match.contracted_services)
            if already & set(criteria.exclude_proposed_services):
                return False

    return True


# ---------------------------------------------------------------------------
# 商材適合
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProductFit:
    """この施設にこの商材が向いている、という判定と根拠。

    `reason`は営業がそのままトークに使える日本語にする(画面とCSVに出す)。
    """

    product: str
    reason: str


# リピッテホテルは客室数で月額が決まるため、レンジから提案額をその場で出せる
# ([[reference_cnctor_product_details]]の公式料金)。
_REPITTE_PRICING: tuple[tuple[int, int | None, str], ...] = (
    (1, 10, "初期29,800円＋月額6,800円"),
    (10, 30, "月額9,800円"),
    (30, 50, "月額13,800円"),
    (50, None, "要問合せ"),
)


def _repitte_price(room_count: int) -> str:
    for minimum, maximum, label in _REPITTE_PRICING:
        if room_count >= minimum and (maximum is None or room_count < maximum):
            return label
    return "要問合せ"


def suggest_products(facility: Facility) -> tuple[ProductFit, ...]:
    """施設データから機械的に言える範囲で、提案候補の商材と根拠を並べる。

    **施設データから確実に言えることだけを根拠にする。** 「たぶん困っているはず」という
    推測は入れない(営業が根拠として読めないものは載せない方がよい)。
    """
    fits: list[ProductFit] = []

    if facility.custom_page_status is CustomPageStatus.NOT_PUBLISHED:
        fits.append(
            ProductFit(
                "WEB制作（楽天CP）",
                "楽天トラベルのカスタマイズページが1ページも公開されていない",
            )
        )
        fits.append(
            ProductFit("クリエイティブラボ", "楽天カスタマイズページ未公開＝制作提案の余地がある")
        )

    score = facility.review_average
    review_count = facility.review_count or 0
    if score is not None and score < Decimal("4.0") and review_count >= 50:
        fits.append(
            ProductFit(
                "フルスコ",
                f"クチコミ点数{score}（{review_count}件）。4.0未満で母数も十分あり改善余地が大きい",
            )
        )
        fits.append(
            ProductFit("ホテルラボ レビュー", f"クチコミ{review_count}件に対し点数{score}。返信運用の改善対象")
        )

    if facility.check_in_machine is CheckInMachineStatus.YES:
        fits.append(
            ProductFit(
                "メイリー（QRチェックイン）",
                "チェックイン機の導入を確認済み。既存機をそのまま活かせる",
            )
        )

    if facility.room_count is not None:
        fits.append(
            ProductFit(
                "リピッテホテル",
                f"客室{facility.room_count}室 → {_repitte_price(facility.room_count)}",
            )
        )
        if facility.room_count >= 50:
            fits.append(
                ProductFit(
                    "ホテルラボRM",
                    f"客室{facility.room_count}室。レベニューマネジメントの効果が出る規模",
                )
            )

    if facility.has_onsen:
        fits.append(ProductFit("ホテルラボ", "温泉施設。OTA上の訴求余地が大きい"))

    if facility.photo_count is not None and facility.photo_count < 20:
        fits.append(
            ProductFit(
                "ホテルラボ（OTA写真撮影）",
                f"楽天トラベル掲載写真が{facility.photo_count}枚と少ない",
            )
        )

    if facility.has_facility("レストラン"):
        fits.append(ProductFit("セルフオーダー", "館内にレストランあり"))

    return tuple(fits)


# ---------------------------------------------------------------------------
# チェックイン機の一次判定(施設ページの記述から)
# ---------------------------------------------------------------------------

# 施設ページ・設備欄にこれらが出ていれば「導入あり」と見てよい語。
# メイリーのQRチェックインは**既存のチェックイン機をそのまま活かす**設計のため、
# 機種名(アルメックス等)が出ていればそれ自体が提案根拠になる
# ([[reference_cnctor_product_details]])。
CHECK_IN_MACHINE_KEYWORDS: tuple[str, ...] = (
    "自動チェックイン機",
    "自動チェックイン",
    "セルフチェックイン",
    "チェックイン機",
    "自動精算機",
    "精算機",
    "アルメックス",
    "ＡＬＭＥＸ",
    "ALMEX",
)


@dataclass(frozen=True)
class CheckInMachineFinding:
    """チェックイン機の一次判定の結果と、そう判断した根拠。"""

    status: CheckInMachineStatus
    evidence: str | None = None


def detect_check_in_machine(*texts: str | None) -> CheckInMachineFinding:
    """施設ページの本文・設備欄からチェックイン機の記載を探す。

    **見つからなくても`NO`にはしない。** 楽天トラベルのページに書かれていないことは
    「導入していない」ことの証明にならないため`UNKNOWN`を返す。`NO`を立てられるのは、
    WEB検索や商談で導入していないと確認できた場合だけ。
    """
    for text in texts:
        if not text:
            continue
        normalized = re.sub(r"[\s　]+", "", text)
        for keyword in CHECK_IN_MACHINE_KEYWORDS:
            index = normalized.find(keyword)
            if index >= 0:
                start = max(0, index - 30)
                snippet = normalized[start : index + len(keyword) + 30]
                return CheckInMachineFinding(CheckInMachineStatus.YES, snippet)
    return CheckInMachineFinding(CheckInMachineStatus.UNKNOWN, None)
