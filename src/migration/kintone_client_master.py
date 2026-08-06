"""kintone 取引先マスタ → ① 取引先マスターDB / ② チェーンDB への変換ロジック（04_項目マッピング）。"""

from __future__ import annotations

import logging
from typing import Iterable

from src.db_schema.client_master import CLIENT_MASTER_SCHEMA

logger = logging.getLogger(__name__)

_FALLBACK_CUSTOMER_TYPE = "その他"

# ①取引先マスターDBの「営業ステータス」（必須）を、紐づく④案件管理DBの営業ステータス
# （8種）から縮約するための優先順位マップ（BLOCKER1: kintone取引先マスタ側に対応する
# 「契約進捗状況」等の列が無く、04_項目マッピングにも導出方法の明記が無いため実装者判断）。
# 1つの取引先に複数案件がある場合、最も商談が進んでいるステータスを代表値として採用する
# 方針（営業サマリーとしては「一番良い状態」を見せるのが実用上妥当なため）。
# 優先順位: 契約済 > 商談中(B/C) > 初回接触/提案中/見積提出 > 失注/解約（先頭が最優先）。
_CLIENT_STATUS_FROM_PROJECT_STATUS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("契約済",), "契約"),
    (("商談中(B)", "商談中(C)"), "商談中"),
    (("初回接触", "提案中", "見積提出"), "アプローチ中"),
    (("失注", "解約"), "失注"),
)
_FALLBACK_CLIENT_STATUS_NO_PROJECT = "未アプローチ"


def derive_client_sales_status(project_statuses: Iterable[str]) -> str:
    """紐づく④案件管理DBの営業ステータス群から①取引先マスターDBの営業ステータスを導出する。

    案件が1件も無ければ「未アプローチ」。1件以上あれば `_CLIENT_STATUS_FROM_PROJECT_STATUS`
    の優先順位で最も商談が進んでいるステータスを採用する。詳細な設計判断の理由は
    docs/migration_pipeline_note.md を参照。
    """
    statuses = set(project_statuses)
    if not statuses:
        return _FALLBACK_CLIENT_STATUS_NO_PROJECT
    for keys, result in _CLIENT_STATUS_FROM_PROJECT_STATUS:
        if statuses & set(keys):
            return result
    # 案件はあるが上記いずれの優先順位カテゴリにも一致しない未知のステータスのみの場合。
    # 顧客種別と同様、必須項目のフォールバックとして安全側の「未アプローチ」を採用しログを残す。
    logger.warning(
        "unmapped project status set for client sales status derivation: %r, "
        "falling back to %r",
        statuses,
        _FALLBACK_CLIENT_STATUS_NO_PROJECT,
    )
    return _FALLBACK_CLIENT_STATUS_NO_PROJECT


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
