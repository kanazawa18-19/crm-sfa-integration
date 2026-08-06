"""アクション履歴DBのtitle自由記述からaction_typeを推定するヒューリスティック分類。

アクション履歴DB（`src/db_schema/action.py`）のtitleプロパティ「商談回数・電話回数・
メール回数（何回目）」は表記ゆれの激しい自由記述であり（【電話】N回目、【商談】N回目、
テレアポ↓（担当者名）等）、`src/analytics/`・`src/reports/`側の分析ロジックが期待する
正規化済みのaction_type（"テレアポ"/"訪問商談"/"オンライン商談"/"メール"/"その他"）を
直接は持たない。本モジュールの`classify_action_type`はキーワードマッチングによる
ヒューリスティックな推定であり、正確なアクション種別分類ではない点に注意。

kintone側（`src/migration/action_mapping.py`・`docs/migration_pipeline_note.md`関連）には
正規化された5種のアクション種別（テレアポ／訪問商談／オンライン商談／メール／自動メール）が
既に存在しており、将来的にはそちらを正としてNotion側アクション履歴DBへ統合する移行が
必要（現状は保留タスク。ダッシュボード用の本モジュールは暫定対応）。
"""

from __future__ import annotations


def classify_action_type(title: str | None) -> str:
    """アクション履歴DBのtitle自由記述からaction_typeを推定する（ヒューリスティック）。

    大文字小文字を無視し、以下の優先順位でキーワード判定する（最初にマッチしたものを採用）。
    いずれにもマッチしない場合（titleがNone・空文字の場合も含む）は"その他"を返す。
    """
    if not title:
        return "その他"

    lowered = title.lower()

    if any(keyword.lower() in lowered for keyword in ("テレアポ", "電話", "TEL")):
        return "テレアポ"
    if "訪問" in lowered:
        return "訪問商談"
    if any(keyword.lower() in lowered for keyword in ("WEB商談", "オンライン", "Web", "ZOOM", "Zoom")):
        return "オンライン商談"
    if any(keyword.lower() in lowered for keyword in ("メール", "Mail")):
        return "メール"
    return "その他"
