"""Zoho CRM / CSV → Notion DB への変換ロジック（04_項目マッピング Zoho/CSV行）。"""

from __future__ import annotations

from src.migration._utils import parse_multi_value


def transform_zoho_client_master(record: dict[str, str]) -> dict[str, str]:
    """Zoho 取引先のデータIDを①取引先マスターDBのZoho_IDへ変換する（移行時の突合キー）。"""
    return {"Zoho_ID": record.get("データID", "")}


def transform_zoho_project_relations(record: dict[str, str]) -> dict[str, object]:
    """Zoho 案件の参照ID・提案サービス（テキスト）を④案件管理DBのリレーション解決用データへ変換する。

    実際のNotionリレーション解決（Zoho_ID→Notion主キーの引き当て）はここでは行わず、
    id_mapping経由で解決する前段のデータ整形のみを担う。
    """
    return {
        "_取引先Zoho_ID": record.get("取引先名.id") or None,
        "_連絡先Zoho_ID": record.get("連絡先名.id") or None,
        "_提案サービス名リスト": parse_multi_value(record.get("提案サービス")),
    }


def transform_common_timestamps(record: dict[str, str]) -> dict[str, str | None]:
    """全モジュール共通の作成日時／更新日時を内部プロパティ（created_at/updated_at）へ変換する。"""
    return {
        "created_at": record.get("作成日時") or None,
        "updated_at": record.get("更新日時") or None,
    }
