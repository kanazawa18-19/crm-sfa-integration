"""保存したNotionページJSONから登録内容を表示する。外部通信・書込みなし。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping

from src.calendar_sync.plan import calendar_properties
from src.calendar_sync.tasks import build_task_plan
from src.calendar_sync.todo import payload


def preview_page(page: Mapping[str, Any]) -> dict[str, Any]:
    if page.get("archived") or page.get("in_trash"):
        return {"status": "保留", "reason": "アーカイブ・ゴミ箱の案件"}
    properties = calendar_properties(page.get("properties") or {})
    plan = build_task_plan(properties, str(page["id"]))
    tasks = []
    for task in plan.tasks:
        reason = ('タスクの期限が未入力' if task.due is None else
                  '担当メンバーが未入力' if not task.assignees else None)
        tasks.append({'task_id': task.key, 'todo': payload(task, creating=True),
                      'event': task.event() if reason is None else None,
                      'calendar_hold': reason})
    return {'mode': 'multi_task_preview', 'required_flag': 'PROJECT_ACTION_TODO_ENABLED=true',
            'activation_note': 'このプレビューは有効化後の候補です。環境設定は変更していません。',
            'status': '登録候補' if tasks else '保留', 'tasks': tasks,
            'holds': list(plan.holds) or ([] if tasks else ['アクションが未入力または対応不要のため登録候補はありません']),
            'note': '書込みなし。既存TODOの人編集・完了・作成結果不明、Calendarの接続・旧予定は実行時に別途照合します'}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("page_json", type=Path)
    args = parser.parse_args()
    print(json.dumps(preview_page(json.loads(args.page_json.read_text())), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
