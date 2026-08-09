"""Zoho 取引先 → ①取引先マスターDB への変換ロジック（04_項目マッピング Zoho行）。

実データ確認済み（2026-08-10、37,446件）:
- 「取引先名.id」等のZoho内部リレーションID列は実データではほぼ100%空欄で、
  当初zoho_mapping.pyが前提にしていた「IDで直接リレーションを解決する」設計は使えない。
- 「顧客種別」は96.7%が空欄。値がある場合も「新規」「既存」のようなステータス的な値が
  混じっており、CLIENT_MASTER_SCHEMAの選択肢（業種分類）とは意味が異なる。
  kintone_client_master.normalize_customer_type と同じ「未知の値は"その他"へ
  フォールバック＋ログ警告」方針を流用する。
- 住所・都道府県・郵便番号は「住所」「都道府県」「郵便番号」という素の列名にそのまま
  入っている（「〜（請求先）」という個別の列は実データでは常に空欄だった）。
- 電話番号（0.9%）・Fax（0%）はほぼ空欄。取引先マスターDBの「TEL」「FAX」プロパティ
  （2026-08-10にkintone移行のバグ修正で新規作成）へそのまま反映する。
"""

from __future__ import annotations

import logging
import re
import unicodedata

from src.db_schema.client_master import CLIENT_MASTER_SCHEMA
from src.migration.kintone_client_master import normalize_customer_type

logger = logging.getLogger(__name__)

_FALLBACK_PREFECTURE = None

_CORPORATE_SUFFIX_RE = re.compile(r"(株式会社|\(株\)|（株）|有限会社|\(有\)|（有）)")
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_company_name_basic(name: str | None) -> str:
    """前後の空白のみを除去する軽い正規化（第一段階、完全一致照合用）。"""
    return (name or "").strip()


def normalize_company_name_strong(name: str | None) -> str:
    """全角/半角統一・空白除去・法人格表記ゆれ吸収を行う強い正規化（第一段階の完全一致で
    見つからなかった場合のみ使う第二段階）。

    実データ検証（2026-08-10、Zoho取引先37,446件 vs Notion取引先マスター9,914件）で、
    第一段階の単純一致率30.6%に対し、この正規化を追加することでさらに0.4ポイントの
    一致率向上を確認済み（例:"ストリングスホテル　名古屋"⇔"ストリングスホテル名古屋"、
    "PLAZA IN KANKU HOTEL"⇔"ＰＬＡＺＡ　ＩＮ　ＫＡＮＫＵ　ＨＯＴＥＬ"）。
    """
    normalized = unicodedata.normalize("NFKC", name or "")
    normalized = _WHITESPACE_RE.sub("", normalized)
    normalized = _CORPORATE_SUFFIX_RE.sub("", normalized)
    return normalized.strip()


def normalize_prefecture(raw: str | None) -> str | None:
    """都道府県をNotion側のセレクト値へ正規化する。

    実データでは32,925件中32,924件が既存の選択肢（表記ゆれ含む）にそのまま合致する
    （2026-08-10確認済み）。唯一の例外的な値（"option1"等、明らかな入力ミス）のみ
    未知値として扱い、normalize_customer_typeと同じ方針でNoneへフォールバックしつつ
    ログへ元の値を残す（無言でのフォールバックを避けるため）。
    """
    if not raw or not raw.strip():
        return None
    normalized = raw.strip()
    valid_options = CLIENT_MASTER_SCHEMA.get_property("都道府県").options
    if normalized in valid_options:
        return normalized
    logger.warning("unmapped 都道府県 value %r, falling back to None", raw)
    return _FALLBACK_PREFECTURE


def transform_zoho_client_master(record: dict[str, str]) -> dict[str, str | None]:
    """Zoho 取引先 1レコードを ①取引先マスターDB のプロパティ値へ変換する。

    "zoho_ID" は突合・IDマッピング用の内部値であり、CLIENT_MASTER_SCHEMAには存在しない
    プロパティ名のため、Notionへの実書き込み前に呼び出し側で必ず取り除くこと
    （kintone移行で"kintone_ID"を同様に扱わず本番書き込み時にKeyErrorになったバグが
    過去にあったため、同じ轍を踏まないよう明記する）。
    """
    return {
        "zoho_ID": record.get("データID", ""),
        "取引先名": record.get("取引先名", ""),
        "顧客種別": normalize_customer_type(record.get("顧客種別")),
        "郵便番号": record.get("郵便番号") or None,
        "都道府県": normalize_prefecture(record.get("都道府県")),
        "住所": record.get("住所") or None,
        "TEL": record.get("電話番号") or None,
        "FAX": record.get("Fax") or None,
    }
