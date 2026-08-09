"""kintone 案件管理 → ④ 案件管理DB への変換ロジック（04_項目マッピング）。"""

from __future__ import annotations

from src.db_schema.project import PROJECT_SCHEMA
from src.migration._utils import parse_multi_value

# 契約進捗状況（kintone実データ側の表記）を④案件管理DBの営業ステータス（実Notionスキーマ、
# 既存タスク#32で実データに合わせて全面書き直し済み: 施設契約/解約/リスケ/失注/アポ/Dヨミ/
# Cヨミ/Bヨミ/Aヨミ/トライアル/契約）へ正規化するための対応表。
# 「商談中（A/B/C/D）」はA〜Dヨミ（商談確度ランク）へ細分せず、まとめて「アポ」に統合する
# 方針（2026-08-09、業務判断確認済み）。未掲載の値は strip 後そのまま採用され、
# PROJECT_SCHEMA の有効な選択肢か検証される。
_STATUS_ALIASES: dict[str, str] = {
    "契約済": "契約",
    "商談中（A）": "アポ",
    "商談中（B）": "アポ",
    "商談中（C）": "アポ",
    "商談中（D）": "アポ",
}


def normalize_project_status(kintone_status: str | None) -> str:
    """契約進捗状況（契約済/商談中(A〜D)/失注 等）を④案件管理DBの営業ステータス値へ正規化する。

    営業ステータスは必須項目（RequirementLevel.REQUIRED）で、かつ本モジュールの
    契約日／予想契約日の振り分けロジックが値に直接依存するため、未知の値は
    黙ってフォールバックせず即ValueErrorとする（kintone_client_master.normalize_customer_type
    の任意項目フォールバック方針とは対称的だが、必須かどうかで意図的に使い分けている）。
    """
    normalized = (kintone_status or "").strip()
    canonical = _STATUS_ALIASES.get(normalized, normalized)
    valid_options = PROJECT_SCHEMA.get_property("営業ステータス").options
    if canonical not in valid_options:
        raise ValueError(f"unmapped 契約進捗状況 value: {kintone_status!r}")
    return canonical


def transform_kintone_project(record: dict[str, str]) -> dict[str, object]:
    """kintone 案件管理 1レコードを ④案件管理DB のプロパティ値へ変換する。

    取引先マスター・サービス・商品へのリレーションはこの時点では解決せず、
    後続の解決ステップ用に `_取引先名`（名寄せ用）/ `_サービス名リスト` として残す。
    契約日・予想契約日はNotion側では単一の「契約日 / 予想契約日」DATEプロパティ
    （個別の「契約日」「初期費用（イニシャル）」等のプロパティはPROJECT_SCHEMAに存在しない。
    以前はこれら存在しないキー名で返しており、実書き込み時に確実にKeyErrorで失敗する
    バグだった）に統合されており、確定契約（営業ステータス「契約」）なら契約日、
    それ以外なら予想契約日として同じ値を入れる。
    """
    status = normalize_project_status(record.get("契約進捗状況", ""))
    billing_date = record.get("課金開始予定日") or None

    return {
        "kintone_ID": record.get("レコード番号", ""),
        "_取引先名": record.get("施設名（会社名）", ""),
        "営業ステータス": status,
        "契約日 / 予想契約日": billing_date,
        "月額費用": record.get("提案料金（ランニング）") or None,
        "初期費用": record.get("提案料金（イニシャル）") or None,
        "_サービス名リスト": parse_multi_value(record.get("サービス（ショット）")),
    }
