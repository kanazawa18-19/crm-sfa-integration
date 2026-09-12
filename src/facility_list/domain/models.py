"""リスト作成マシーンのドメインモデル(2026-09-11)。

営業リストの母集団は楽天トラベルの公開施設ページであり、抽出条件・施設・商材適合の
「業務ルール」だけをこの層に置く。HTTP・HTML・Postgres・スプレッドシートには一切
依存しない(AGENTS.mdのDDD方針。Domain層はDBも外部APIも起動せず単体テストできること)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum


class FacilityCategory(str, Enum):
    """施設カテゴリ。

    **楽天トラベルの公開ページにこの分類の項目は存在しない**(2026-09-11に実測で確認。
    検索UIの「宿タイプ」は温泉宿/高級宿の2つだけ)。施設名と設備からの**推定**であり、
    確定情報ではない。推定の確からしさは`CategoryConfidence`で併せて持つ。
    """

    HOTEL = "hotel"  # ホテル(ビジネス/シティ/リゾート)
    RYOKAN = "ryokan"  # 旅館・温泉宿
    PENSION = "pension"  # ペンション・民宿・ゲストハウス・ロッジ
    VILLA = "villa"  # 貸別荘・コテージ・一棟貸し
    OTHER = "other"  # 上記に当てはまらない
    UNKNOWN = "unknown"  # 判定材料が足りない


class CategoryConfidence(str, Enum):
    """カテゴリ推定の確からしさ。`UNKNOWN`と`LOW`を画面で区別して出すために持つ。"""

    HIGH = "high"  # 施設名に決め手の語がある(例: 「ペンション」「旅館」)
    MEDIUM = "medium"  # 設備・部屋構成からの推定
    LOW = "low"  # 弱い手がかりのみ
    NONE = "none"  # 判定できず


class CheckInMachineStatus(str, Enum):
    """チェックイン機(自動チェックイン機)の導入有無。

    **「なし」と「不明」を必ず分ける。** 楽天トラベルの施設ページに記載が無いことは
    「導入していない」ことの証明にならないため、記載が見つからなければ`UNKNOWN`に倒す
    (§1「確認した事実と推測を混ぜない」)。`NO`を立てるのは、WEB検索や商談で
    「導入していない」と確認できた場合だけ。
    """

    YES = "yes"
    NO = "no"
    UNKNOWN = "unknown"


class CheckInMachineSource(str, Enum):
    """チェックイン機の判定根拠。どこで分かったかを必ず残す。"""

    FACILITY_PAGE = "facility_page"  # 楽天トラベルの施設ページの記述
    WEB_SEARCH = "web_search"  # WEB検索(施設公式サイト・ニュース等)
    CRM = "crm"  # 商談履歴・CRMの手入力
    NONE = "none"


class CustomPageStatus(str, Enum):
    """楽天カスタマイズページの公開状態。

    施設ページのHTMLに `GW{施設番号}{連番}.html` へのリンクがあれば公開中と判定する
    (2026-09-11、大阪市30施設で実測。18施設が公開あり/12施設が無し)。
    「未公開で保管中」は外からは見えないため、`NOT_PUBLISHED`は
    「公開されているページが1つも無い」という意味であり、「1ページも作っていない」の
    証明ではない。
    """

    PUBLISHED = "published"
    NOT_PUBLISHED = "not_published"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RoomCountRange:
    """客室数レンジ(◯室以上◯室未満)。既定は1室以上・上限なし。"""

    minimum: int = 1
    maximum: int | None = None  # Noneは上限なし

    def __post_init__(self) -> None:
        if self.minimum < 0:
            raise ValueError("客室数の下限に負の値は指定できない")
        if self.maximum is not None and self.maximum <= self.minimum:
            raise ValueError("客室数の上限は下限より大きい値を指定する(◯室以上◯室未満)")

    def contains(self, room_count: int | None) -> bool:
        """客室数が不明(None)の施設はレンジに含めない。

        既定(1室以上・上限なし)であっても`None`は除外する。「不明」を「条件を満たす」
        として混ぜると、客室数で料金が決まるリピッテの提案額が出せないリストになるため。
        """
        if room_count is None:
            return False
        if room_count < self.minimum:
            return False
        if self.maximum is not None and room_count >= self.maximum:
            return False
        return True


@dataclass(frozen=True)
class ReviewScoreRange:
    """クチコミ点数レンジ(◯.◯以上◯.◯未満)。楽天の総合点は1.00〜5.00。"""

    minimum: Decimal | None = None
    maximum: Decimal | None = None
    include_unrated: bool = False  # クチコミが無い施設(点数None)を含めるか

    def __post_init__(self) -> None:
        if (
            self.minimum is not None
            and self.maximum is not None
            and self.maximum <= self.minimum
        ):
            raise ValueError("クチコミ点数の上限は下限より大きい値を指定する")

    def contains(self, score: Decimal | None) -> bool:
        if score is None:
            return self.include_unrated
        if self.minimum is not None and score < self.minimum:
            return False
        if self.maximum is not None and score >= self.maximum:
            return False
        return True


@dataclass(frozen=True)
class Facility:
    """楽天トラベルに掲載されている宿泊施設1軒。

    値の出どころは全て公開ページ(`travel.rakuten.co.jp/HOTEL/{hotelNo}/`)であり、
    取得できなかった項目は推測で埋めず`None`のままにする。
    """

    hotel_no: int
    name: str
    address: str | None = None
    postal_code: str | None = None
    prefecture: str | None = None
    city: str | None = None
    room_count: int | None = None
    review_average: Decimal | None = None
    review_count: int | None = None
    photo_count: int | None = None
    # エリア一覧に出ている最安料金(円・税別表示)。規模感と価格帯の目安に使う。
    min_charge: int | None = None
    # 施設の電話番号。**楽天の公開ページには出ないので取り込みでは常にNone。**
    # リストに載る電話番号はCRM側の取引先マスターから取っている
    # (`CrmMatch.client_phone`、`exporters.row_to_values()`参照)。
    # 将来ほかの経路(公式サイト等)で取れるようになったときのために口だけ開けてある。
    telephone: str | None = None
    category: FacilityCategory = FacilityCategory.UNKNOWN
    category_confidence: CategoryConfidence = CategoryConfidence.NONE
    has_onsen: bool = False
    custom_page_status: CustomPageStatus = CustomPageStatus.UNKNOWN
    custom_page_count: int = 0
    check_in_machine: CheckInMachineStatus = CheckInMachineStatus.UNKNOWN
    check_in_machine_source: CheckInMachineSource = CheckInMachineSource.NONE
    facilities: tuple[str, ...] = ()  # 館内設備
    room_facilities: tuple[str, ...] = ()  # 部屋設備・備品
    is_listed: bool = True  # 楽天トラベルに掲載中か(掲載終了でも行は消さない)

    @property
    def page_url(self) -> str:
        return f"https://travel.rakuten.co.jp/HOTEL/{self.hotel_no}/{self.hotel_no}.html"

    def has_facility(self, keyword: str) -> bool:
        """館内設備・部屋設備に指定の語を含むか(部分一致)。"""
        return any(keyword in f for f in self.facilities) or any(
            keyword in f for f in self.room_facilities
        )


class CrmMatchState(str, Enum):
    """CRM(Notion取引先マスター)との突合結果。

    **4つを混ぜない。** 突合していないものを「未取引」として営業に渡すと、既存顧客へ
    新規営業をかける事故になる(§1「確認した事実と推測を混ぜない」)。

    `NO_NAME_MATCH`の値名は互換性のため維持する。現在は、同期済みの名前・住所・電話に
    一致しなかった状態。二次項目の取得が全行で終わり、施設側にも照合材料がある場合のみ
    返す。CRMに施設の所在地が未登録の運営会社は取りこぼすため、未取引の証明ではない。
    """

    MATCHED = "matched"  # 1件に確定
    AMBIGUOUS = "ambiguous"  # 候補あり。単独候補でも根拠不足なら人が確認する
    NO_NAME_MATCH = "no_name_match"  # 登録情報で一致なし。値名は旧APIとの互換用
    NOT_CHECKED = "not_checked"  # まだ突合していない


class NameMatchStrength(str, Enum):
    """施設名のどの候補で当たったか。

    弱い候補(空白区切りの最後の塊)だけで確定させると、「ホテルABC 大阪」の「大阪」が
    別会社に1件だけ当たって、**その会社の担当者名・メール・電話が別施設の行に出る**
    (ChatGPTレビューのBLOCKER、2026-09-12)。弱い候補は住所の一致を必須にする。
    """

    EXACT = "exact"  # 施設名そのまま(空白の有無だけ違う)
    STRIPPED = "stripped"  # 先頭の修飾語(「天然温泉」等)を落としたもの
    WEAK = "weak"  # 空白区切りの最後の塊。単独では確定させない


@dataclass(frozen=True)
class CrmContact:
    """CRM側の担当者連絡先(Notion連絡先DBの1件)。"""

    contact_page_id: str
    name: str | None = None
    email: str | None = None
    phone: str | None = None
    title: str | None = None  # 役職


@dataclass(frozen=True)
class CrmMatch:
    """施設1軒に対するCRM突合の結果。"""

    state: CrmMatchState
    client_page_id: str | None = None
    client_name: str | None = None
    owner_name: str | None = None  # 自社の担当営業
    client_phone: str | None = None  # 取引先マスターのTEL
    client_fax: str | None = None  # 取引先マスターのFAX
    proposed_services: tuple[str, ...] = ()  # 提案済みサービス
    contracted_services: tuple[str, ...] = ()  # 取引中サービス
    contacts: tuple[CrmContact, ...] = ()
    candidate_names: tuple[str, ...] = ()  # AMBIGUOUSのときの候補
    # どの強さの名前候補で当たったか。MATCHEDのときだけ入る。
    matched_by: "NameMatchStrength | None" = None
    evidence: tuple[str, ...] = ()  # 照合根拠。住所だけ・電話だけの候補もここに残す。


class CrmFilter(str, Enum):
    """CRM突合の状態による絞り込み。"""

    ANY = "any"
    NEW_ONLY = "new_only"  # 未取引(CRMに無い)だけ
    EXISTING_ONLY = "existing_only"  # 既存取引先だけ


@dataclass(frozen=True)
class ListCriteria:
    """リストの抽出条件。画面のフォーム1回分に相当する。

    全ての条件は**AND**で効く。未指定の項目は絞り込まない。
    """

    prefectures: tuple[str, ...] = ()  # 空なら全国
    room_count: RoomCountRange = field(default_factory=RoomCountRange)
    review_score: ReviewScoreRange = field(default_factory=ReviewScoreRange)
    categories: tuple[FacilityCategory, ...] = ()  # 空なら問わない
    custom_page: CustomPageStatus | None = None  # Noneなら問わない
    check_in_machine: CheckInMachineStatus | None = None
    has_onsen: bool | None = None
    min_review_count: int | None = None
    max_photo_count: int | None = None  # 写真がこの枚数未満の施設だけ
    crm_filter: CrmFilter = CrmFilter.ANY
    exclude_proposed_services: tuple[str, ...] = ()  # この商材を提案済みの施設を除く
    exclude_chains: bool = False  # チェーン系を除く
    limit: int | None = None
