"""カレンダーイベントのタイトルから、アクション履歴DBの`アクション種別`を推定する。

命名規則（2026-08-13、金沢さん確認済み）: タイトルの【】内に「訪問」または「WEB」等が
書かれる（例:「【商談（訪問）】〜〜ホテル」「【商談（WEB）】〜〜ホテル様」）。

`アクション種別`はNotion側でRequirementLevel.REQUIRED（`src/db_schema/action.py`）のため、
本モジュールは必ず何らかの値を返す設計とする（パターンにマッチしない場合はGoogle Meet
リンクの有無でフォールバックする）。
"""

from __future__ import annotations

import re

_VISIT_ACTION_TYPE = "訪問商談"
_ONLINE_ACTION_TYPE = "オンライン商談"

_BRACKET_PATTERN = re.compile(r"【[^】]*(訪問|WEB|Web|web|オンライン)[^】]*】")


def infer_action_type(event_title: str, *, has_meet_link: bool) -> str:
    """`event_title`からアクション種別を推定する（訪問商談/オンライン商談のいずれかを返す）。"""
    match = _BRACKET_PATTERN.search(event_title or "")
    if match:
        keyword = match.group(1)
        if keyword == "訪問":
            return _VISIT_ACTION_TYPE
        return _ONLINE_ACTION_TYPE

    # 【】パターンにマッチしない場合はGoogle Meetリンクの有無でフォールバックする
    # （アクション種別は必須のため空欄にはしない。営業担当はSlack承認時にNotion側で
    # 修正できる）。
    return _ONLINE_ACTION_TYPE if has_meet_link else _VISIT_ACTION_TYPE
