"""Zoho 連絡先 → ③連絡先DB への変換ロジック（04_項目マッピング Zoho行）。

実データ確認済み（2026-08-10、3,781件）:
- 「取引先名.id」「会社名（母体）.id」「施設名.id」等、取引先への直接リレーションIDは
  全て0%（実データでは一度も使われていない）。代わりに「【Eight】会社名」という自由記述の
  会社名列が77.1%埋まっている（Eightの名刺交換データがそのままZohoへ取り込まれた形跡）。
  この自由記述を、①取引先マスターの名寄せロジック（notion_dedupe.py、zoho_client_master.py
  で実装済み）へそのまま流用して取引先マスターDBとの関連付けに使う。
- 「名刺交換日」「【Eight】名刺交換者」等のEight由来データも実データに存在するが、
  連絡先DB（CONTACT_SCHEMA）の「名刺交換日」「名刺交換者」「Eight連携ID」「人事異動フラグ」
  は、いずれも `RequirementLevel.AUTO` かつ「Eight連携で自動投入」という説明が付いており、
  現在保留中のEight連携機能（タスク#37）専用に設計されたプロパティである。今回のZoho移行
  では意図的に書き込まず空欄のままにする（金沢さん確認済み: 将来のEight連携機能が
  このデータと衝突しないようにするため）。
"""

from __future__ import annotations


def transform_zoho_contact(record: dict[str, str]) -> dict[str, str | None]:
    """Zoho 連絡先 1レコードを ③連絡先DB のプロパティ値へ変換する。

    取引先マスターへのリレーションはこの時点では解決せず、後続の解決ステップ用に
    `_会社名`（notion_dedupe.match_existing_client()での名寄せ用）のまま残す。
    "zoho_ID" はIDマッピング専用の内部値であり、CONTACT_SCHEMAには存在しないプロパティ
    名のため、Notionへの実書き込み前に呼び出し側で必ず取り除くこと。
    """
    return {
        "zoho_ID": record.get("データID", ""),
        "名前": record.get("氏名", ""),
        "部署": record.get("部署名") or None,
        "役職": record.get("役職") or None,
        "メールアドレス": record.get("メール") or record.get("e-mail") or None,
        "携帯番号": record.get("携帯電話") or None,
        "直通TEL": record.get("TEL会社") or None,
        "_会社名": record.get("【Eight】会社名") or None,
    }
