"""Zoho チェーン → ②チェーンDB への変換ロジック（04_項目マッピング Zoho行）。

実データ確認済み（2026-08-10、232件）: Zohoの「アプローチ状況」列は、CHAIN_SCHEMAの
STATUS選択肢（未アプローチ/連絡済み（アポNG）等13種類）とほぼ完全に一致する
（232件中未知の値は「提案中」1件のみ）。取引先マスターのnormalize_customer_typeと
同じ「未知の値はログ警告＋Noneへフォールバック」方針を適用する。
"""

from __future__ import annotations

import logging

from src.db_schema.chain import CHAIN_SCHEMA
from src.migration._utils import normalize_date

logger = logging.getLogger(__name__)


def normalize_approach_status(raw: str | None) -> str | None:
    """アプローチ状況をNotion側のステータス値へ正規化する。"""
    if not raw or not raw.strip():
        return None
    normalized = raw.strip()
    valid_options = CHAIN_SCHEMA.get_property("アプローチ状況").options
    if normalized in valid_options:
        return normalized
    logger.warning("unmapped アプローチ状況 value %r, falling back to None", raw)
    return None


def transform_zoho_chain(record: dict[str, str]) -> dict[str, str | None]:
    """Zoho チェーン 1レコードを ②チェーンDB のプロパティ値へ変換する。

    "zoho_ID" はIDマッピング専用の内部値であり、CHAIN_SCHEMAには存在しないプロパティ
    名のため、Notionへの実書き込み前に呼び出し側で必ず取り除くこと。
    """
    return {
        "zoho_ID": record.get("データID", ""),
        "グループ名": record.get("チェーン名・グループ名", ""),
        "アプローチ状況": normalize_approach_status(record.get("アプローチ状況")),
        "施設数": record.get("施設数") or None,
        "本社": record.get("本社") or None,
        "本社所在地": record.get("本社所在地") or None,
        "運営会社": record.get("運営会社") or None,
        "電話": record.get("電話") or None,
        "URL": record.get("チェーンURL") or None,
        "メモ": record.get("メモ") or None,
        "決裁": record.get("決裁") or None,
        "未導入店舗へのアプローチ": record.get("未導入店へのアプローチ") or None,
        "自動チェックインURL": record.get("自動チェックイン機（URL）") or None,
        "自動チェックイン": record.get("自動チェックイン機") or None,
        "最終アプローチ日": normalize_date(record.get("最終更新日（最終アプローチ日）")),
    }
