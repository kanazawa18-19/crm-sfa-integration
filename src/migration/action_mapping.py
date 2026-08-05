"""kintone アクション管理 → ⑥ アクション管理DB への変換ロジック（04_項目マッピング）。"""

from __future__ import annotations

from src.db_schema.action import ACTION_SCHEMA
from src.migration._utils import parse_multi_value

# kintone「アクション内容」の表記ゆれをNotion側セレクト値へ正規化するための対応表。
_ACTION_TYPE_ALIASES: dict[str, str] = {
    "電話": "テレアポ",
    "訪問": "訪問商談",
    "Web商談": "オンライン商談",
    "web商談": "オンライン商談",
}


def normalize_action_type(kintone_action: str | None) -> str:
    """アクション内容（訪問商談/テレアポ 等）を⑥アクション管理DBのアクション種別値へ正規化する。

    アクション種別は必須項目（RequirementLevel.REQUIRED）のため、project_mapping.
    normalize_project_status と同様に未知の値はフォールバックせずValueErrorとする
    （必須項目は例外方式、任意の分類項目のみログ付きフォールバック方式、で統一している）。
    """
    normalized = (kintone_action or "").strip()
    canonical = _ACTION_TYPE_ALIASES.get(normalized, normalized)
    valid_options = ACTION_SCHEMA.get_property("アクション種別").options
    if canonical not in valid_options:
        raise ValueError(f"unmapped アクション内容 value: {kintone_action!r}")
    return canonical


def transform_kintone_action(record: dict[str, str]) -> dict[str, object]:
    """kintone アクション管理 1レコードを ⑥アクション管理DB のプロパティ値へ変換する。

    担当営業・先方担当者・提案サービスはこの時点ではリレーション解決せず、
    後続の解決ステップ用に `_` プレフィックス付きの氏名／名称のまま残す。
    """
    return {
        "kintone_Act_ID": record.get("レコード番号", ""),
        "アクション種別": normalize_action_type(record.get("アクション内容", "")),
        "履歴メモ": record.get("コメント") or None,
        "_担当営業氏名": record.get("対応者", ""),
        "_先方担当者氏名": record.get("担当者名") or None,
        "_提案サービス名リスト": parse_multi_value(record.get("提案サービス")),
    }


def extract_next_action_date_for_project(record: dict[str, str]) -> str | None:
    """アクション管理の次回アクション日を、紐づく④案件管理DBの次回アクション日へ反映する値として抽出する。

    04_項目マッピングにおいて、次回アクション日はアクション管理アプリ由来だが
    反映先は案件管理DBであるため、アクション種別の変換とは別関数として切り出す。
    """
    return record.get("次回アクション日") or None
