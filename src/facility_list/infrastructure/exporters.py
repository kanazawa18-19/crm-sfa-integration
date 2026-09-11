"""リストの出力(2026-09-11)。CSVとスプレッドシート用の2次元配列を作る。

列の並びは本人指定(2026-09-11)。**そのまま架電管理表として使える形**にしてあり、
左側に営業が手で埋める欄(コールステータス・メモ・姿勢・日付)を置き、右側に機械が
埋めた事実を並べる。

`顕在課題`だけは空欄にせず、施設データから機械的に言えることを初期値として入れる
(営業が上書きしてよい)。「クチコミ3.8で1,200件」のような**根拠のある一文**があると
架電の一言目が決まるため。
"""

from __future__ import annotations

import csv
import io
from datetime import datetime

from src.facility_list.application.build_list import ListResult, ListRow
from src.facility_list.domain.models import CrmMatchState
from src.facility_list.domain.services import chain_name_of

# 営業が手で埋める欄。出力時は空欄のまま置く。
MANUAL_COLUMNS: tuple[str, ...] = ("コールステータス", "メモ", "姿勢", "日付")

HEADERS: tuple[str, ...] = (
    "施設名",
    "コールステータス",
    "担当",
    "電話番号",
    "メモ",
    "姿勢",
    "顕在課題",
    "現状の取引",
    "URL",
    "施設カテゴリー",
    "客室数",
    "最低料金",
    "エリア",
    "都道府県",
    "チェーン",
    "楽天口コミ点数",
    "楽天口コミ数",
    "FAX番号",
    "メール",
    "作成者",
    "作成日時",
    "日付",
    # ここから右は補助情報(架電には使わないが、判断の根拠として残す)
    "カテゴリー推定の確からしさ",
    "楽天カスタマイズページ",
    "温泉",
    "写真枚数",
    "チェックイン機",
    "CRM状態",
    "取引先名",
    "提案済みサービス",
    "担当者名",
    "役職",
    "提案候補商材",
)

# 画面のプレビュー表に出す列。`HEADERS`の並びを変えても壊れないよう**名前で**持つ。
# 営業が条件を詰めている最中に見たいものだけに絞り、残りはCSVで見てもらう。
# 「カテゴリー推定の確からしさ」と「CRM状態」を入れているのは、推定と事実の区別を
# 行単位で見せるため(obasan-qualityレビュー指摘、2026-09-11)。
PREVIEW_COLUMNS: tuple[str, ...] = (
    "施設名",
    "都道府県",
    "エリア",
    "施設カテゴリー",
    "カテゴリー推定の確からしさ",
    "客室数",
    "最低料金",
    "楽天口コミ点数",
    "楽天口コミ数",
    "写真枚数",
    "温泉",
    "楽天カスタマイズページ",
    "チェックイン機",
    "チェーン",
    "CRM状態",
    "顕在課題",
)

_CRM_STATE_LABELS = {
    CrmMatchState.MATCHED: "既存取引先",
    CrmMatchState.AMBIGUOUS: "要確認(候補が複数)",
    # **「未取引」と言い切らない。** 名前照合で当たらなかっただけで、運営会社名で
    # 登録されている既存顧客の可能性が残る(他社レビュー指摘、2026-09-12)。
    CrmMatchState.NO_NAME_MATCH: "未取引の可能性（名前照合のみ）",
    CrmMatchState.NOT_CHECKED: "未突合",
}

_CUSTOM_PAGE_LABELS = {
    "published": "公開あり",
    "not_published": "未作成",
    "unknown": "未確認",
}

_CHECK_IN_LABELS = {"yes": "あり", "no": "なし", "unknown": "不明"}

_CATEGORY_LABELS = {
    "hotel": "ホテル",
    "ryokan": "旅館・温泉宿",
    "pension": "ペンション・民宿",
    "villa": "貸別荘・コテージ",
    "other": "その他",
    "unknown": "不明",
}

_CONFIDENCE_LABELS = {"high": "高", "medium": "中", "low": "低", "none": "—"}


def build_issue_text(row: ListRow) -> str:
    """「顕在課題」列に入れる文。施設データから言えることだけを並べる。

    推測(「人手が足りないはず」等)は入れない。読んだ営業がそのまま先方に言える
    事実だけにする。
    """
    f = row.facility
    issues: list[str] = []

    if f.custom_page_status.value == "not_published":
        issues.append("楽天カスタマイズページが未作成")
    if f.review_average is not None and f.review_count:
        if f.review_average < 4.0:
            issues.append(f"クチコミ{f.review_average}（{f.review_count}件）と伸びしろあり")
    if f.photo_count is not None and f.photo_count < 20:
        issues.append(f"掲載写真が{f.photo_count}枚と少ない")
    return " / ".join(issues)


def row_to_values(
    row: ListRow, *, created_by: str = "", created_at: datetime | None = None
) -> list[str]:
    """1行を文字列の配列にする。

    **空欄は空欄のまま出す。** 「不明」を「なし」に書き換えない(営業が誤って
    「導入していない施設」として扱わないため)。
    """
    f = row.facility
    crm = row.crm
    contact = crm.contacts[0] if crm.contacts else None
    stamp = (created_at or datetime.now()).strftime("%Y-%m-%d %H:%M")

    phone = f.telephone or crm.client_phone or ""
    email = (contact.email if contact and contact.email else "") or ""

    return [
        f.name,
        "",  # コールステータス(営業が埋める)
        crm.owner_name or "",
        phone,
        "",  # メモ
        "",  # 姿勢
        build_issue_text(row),
        " / ".join(crm.contracted_services),
        f.page_url,
        _CATEGORY_LABELS.get(f.category.value, f.category.value),
        str(f.room_count) if f.room_count is not None else "",
        str(f.min_charge) if f.min_charge is not None else "",
        f.city or "",
        f.prefecture or "",
        chain_name_of(f) or "",
        str(f.review_average) if f.review_average is not None else "",
        str(f.review_count) if f.review_count is not None else "",
        crm.client_fax or "",
        email,
        created_by,
        stamp,
        "",  # 日付(架電日など。営業が埋める)
        _CONFIDENCE_LABELS.get(f.category_confidence.value, ""),
        _CUSTOM_PAGE_LABELS.get(f.custom_page_status.value, ""),
        "あり" if f.has_onsen else "",
        str(f.photo_count) if f.photo_count is not None else "",
        _CHECK_IN_LABELS.get(f.check_in_machine.value, ""),
        _CRM_STATE_LABELS.get(crm.state, ""),
        crm.client_name or (" / ".join(crm.candidate_names) if crm.candidate_names else ""),
        " / ".join(crm.proposed_services),
        contact.name if contact and contact.name else "",
        contact.title if contact and contact.title else "",
        " / ".join(row.products),
    ]


def to_rows(
    result: ListResult, *, created_by: str = "", created_at: datetime | None = None
) -> list[list[str]]:
    """ヘッダー込みの2次元配列。スプレッドシート書き込みにそのまま渡せる。"""
    stamp = created_at or datetime.now()
    return [list(HEADERS)] + [
        row_to_values(row, created_by=created_by, created_at=stamp) for row in result.rows
    ]


# Excel/Googleスプレッドシートはセルが `=` `+` `-` `@` で始まると数式として解釈する。
# 施設名は楽天トラベル(外部サイト)の登録内容がそのまま入るため、細工された名前が
# 混じると営業の手元で数式が実行されうる(shirokuma-secレビュー指摘、2026-09-11)。
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def escape_formula(value: str) -> str:
    """数式として解釈されうるセルの先頭にシングルクォートを付ける。

    表示上は元の文字列のまま見える(Excelが先頭のクォートを表示しない)ので、
    営業が読む分には違いが分からない。
    """
    if value and value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value


def to_csv(
    result: ListResult, *, created_by: str = "", created_at: datetime | None = None
) -> str:
    """Excelでそのまま開けるCSV文字列(BOM付きUTF-8)。

    BOMを付けないとExcelがShift_JISと誤認して日本語が化ける。
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    for row in to_rows(result, created_by=created_by, created_at=created_at):
        writer.writerow([escape_formula(cell) for cell in row])
    return "﻿" + buffer.getvalue()


def preview_column_indexes() -> tuple[int, ...]:
    """`PREVIEW_COLUMNS`が`HEADERS`の何番目かを返す。

    `HEADERS`に存在しない列名を`PREVIEW_COLUMNS`に書いてしまったら、ここで
    `ValueError`になる(静かに違う列が出るより、気づける方がよい)。
    """
    return tuple(HEADERS.index(name) for name in PREVIEW_COLUMNS)
