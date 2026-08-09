"""Zoho サービス・商品 → ⑤サービス・商品DB への変換ロジック（04_項目マッピング Zoho行）。

実データ確認済み（2026-08-10、260件）:
- 「案件.id」「先方担当者.id」等のリレーションIDは10.0%のみ入力あり。
- 課金形態（月額ストック/イニシャルスポット/成果報酬、PRODUCT_SCHEMAでREQUIRED）に対応する
  列がZoho側に存在しないため、kintone移行時と同じ方針で暫定的に「イニシャルスポット」を
  既定値とする（実データ精査後に手動調整する前提、2026-08-09業務判断確認済みの方針を踏襲）。
"""

from __future__ import annotations

_DEFAULT_BILLING_TYPE = "イニシャルスポット"


def transform_zoho_product(record: dict[str, str]) -> dict[str, str | float | None]:
    """Zoho サービス・商品 1レコードを ⑤サービス・商品DB のプロパティ値へ変換する。

    "zoho_ID" はIDマッピング専用の内部値であり、PRODUCT_SCHEMAには存在しないプロパティ
    名のため、Notionへの実書き込み前に呼び出し側で必ず取り除くこと。
    """
    initial_fee = record.get("初期費用") or None
    monthly_fee = record.get("月額費用") or None
    return {
        "zoho_ID": record.get("データID", ""),
        "名前": record.get("サービス・商品名", ""),
        "課金形態": _DEFAULT_BILLING_TYPE,
        "標準初期費用": float(initial_fee) if initial_fee is not None else None,
        "標準月額費用": float(monthly_fee) if monthly_fee is not None else None,
    }
