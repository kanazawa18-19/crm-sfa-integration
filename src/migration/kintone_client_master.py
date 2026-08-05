"""kintone 取引先マスタ → ① 取引先マスターDB / ② チェーンDB への変換ロジック（04_項目マッピング）。"""

from __future__ import annotations

import logging

from src.db_schema.client_master import CLIENT_MASTER_SCHEMA

logger = logging.getLogger(__name__)

_FALLBACK_CUSTOMER_TYPE = "その他"


def normalize_customer_type(raw: str | None) -> str | None:
    """顧客種別をNotion側のセレクト値へ正規化する。

    未知の値は『その他』にフォールバックする（project_mapping/action_mappingの
    正規化関数はValueErrorで即エラーにするのに対し、非対称に見えるがこれは意図的）。
    顧客種別は分類用の任意項目（RequirementLevel.OPTIONAL）で後続の分岐ロジックに
    影響しないため、1件のゆらぎで一括移行バッチ全体を止めるより継続を優先する。
    ただし無言のフォールバックは調査を困難にするため、必ずログへ元の値を残す。
    """
    if not raw or not raw.strip():
        return None
    normalized = raw.strip()
    valid_options = CLIENT_MASTER_SCHEMA.get_property("顧客種別").options
    if normalized in valid_options:
        return normalized
    logger.warning(
        "unmapped 顧客種別 value %r, falling back to %r", raw, _FALLBACK_CUSTOMER_TYPE
    )
    return _FALLBACK_CUSTOMER_TYPE


def transform_client_master(record: dict[str, str]) -> dict[str, str | None]:
    """kintone 取引先マスタ 1レコードを ①取引先マスターDB のプロパティ値へ変換する。"""
    return {
        "kintone_ID": record.get("レコード番号", ""),
        "取引先名": record.get("顧客名（法人・個人・施設）", ""),
        "顧客種別": normalize_customer_type(record.get("顧客種別")),
        "郵便番号": record.get("〒") or None,
        "都道府県": record.get("都道府県") or None,
        "住所": record.get("住所") or None,
        "TEL": record.get("TEL") or None,
        "FAX": record.get("FAX") or None,
    }


def extract_chain_name(record: dict[str, str]) -> str | None:
    """本部名から②チェーンDBのチェーン名を抽出する。未入力ならチェーンDBは生成しない。"""
    chain_name = (record.get("本部名") or "").strip()
    return chain_name or None
