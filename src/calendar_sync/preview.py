"""保存したNotionページJSONから登録内容を表示する。外部通信・書込みなし。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping

from src.calendar_sync.plan import build_event_plan, calendar_properties


def preview_page(page: Mapping[str, Any]) -> dict[str, Any]:
    if page.get("archived") or page.get("in_trash"):
        return {"status": "保留", "reason": "アーカイブ・ゴミ箱の案件"}
    properties = calendar_properties(page.get("properties") or {})
    plan = build_event_plan(properties, str(page["id"]))
    if plan is None:
        return {"status": "保留", "reason": "アクション内容・AI推奨アクション日の未入力、またはアクションなしの指定"}
    reps = properties.get("担当メンバー") or []
    if not reps:
        return {"status": "保留", "reason": "担当メンバーが未入力"}
    return {
        "status": "登録候補", "event": plan, "notion_user_id": reps[0],
        "note": "終日予定。担当者のメール解決・カレンダー接続は別途確認が必要",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("page_json", type=Path)
    args = parser.parse_args()
    print(json.dumps(preview_page(json.loads(args.page_json.read_text())), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
