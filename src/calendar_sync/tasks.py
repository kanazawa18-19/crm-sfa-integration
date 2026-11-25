"""案件の明示されたアクション境界と年月日だけを使う、外部I/Oなしの判断。"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import date
import hashlib
import re
import unicodedata
from typing import Any, Mapping
from uuid import UUID
from src.calendar_sync.plan import select_action_date

_NONE = {'なし', 'アクションなし', '対応不要', '該当なし', '未入力', '-', '—'}
_BULLET = re.compile(r'^\s*(?:[-*•・]\s*|\d+[.)、]\s+)(?:\[ \]\s*)?', re.MULTILINE)
_DATE = re.compile(r'(?<!\d)(?:\d{4}-\d{2}-\d{2}|\d{4}/\d{1,2}/\d{1,2}|\d{4}年\d{1,2}月\d{1,2}日)(?![A-Za-z0-9])')
_DATE_WITH_WEEKDAY = re.compile(_DATE.pattern + r'(?:\s*[（(][月火水木金土日](?:曜(?:日)?)?[）)])?')
_ID = re.compile(r'^\[id:([A-Za-z0-9_-]{1,64})\]\s*', re.IGNORECASE)


@dataclass(frozen=True)
class ActionTask:
    key: str
    title: str
    due: str | None
    project_id: str
    project_title: str
    source: str
    assignees: tuple[str, ...]

    def event(self) -> dict[str, str]:
        if self.due is None:
            raise ValueError('期限のないタスクは予定にできません')
        short = ' '.join(self.title.split())
        return {'task_id': self.key, 'notion_project_id': self.project_id,
                'summary': f'{self.project_title} - {short[:100]}' + ('…' if len(short) > 100 else ''),
                'date': self.due,
                'description': f'{self.source}\n{self.title}\n\nNotion案件: https://www.notion.so/{UUID(self.project_id).hex}'}


@dataclass(frozen=True)
class TaskPlan:
    tasks: tuple[ActionTask, ...]
    holds: tuple[str, ...] = ()


def split_actions(text: str) -> list[str]:
    """行頭の明示箇条書きだけを分割。説明文や自然文の句読点を推測で分けない。"""
    markers = list(_BULLET.finditer(text))
    if not markers:
        return [text.strip()]
    prefix = text[:markers[0].start()].strip()
    if prefix and not (len(prefix.splitlines()) == 1 and prefix.endswith((':', '：'))):
        raise ValueError('前置きと箇条書きの境界が曖昧です。前置きは末尾をコロンにしてください')
    return [text[m.end():markers[i + 1].start() if i + 1 < len(markers) else len(text)].strip()
            for i, m in enumerate(markers)]


def build_task_plan(properties: Mapping[str, Any], project_id: str, *, today: date | None = None) -> TaskPlan:
    project_id = str(UUID(project_id))
    source, action = '', ''
    for name in ('次回アクション', 'AI推奨アクション'):
        value = properties.get(name)
        if isinstance(value, str) and value.strip():
            source, action = name, value.strip()
            break
    if not action or action in _NONE:
        return TaskPlan(())
    try:
        items = split_actions(action)
    except ValueError as exc:
        return TaskPlan((), (str(exc),))
    if len(items) > 50:
        return TaskPlan((), ('タスク数が50件を超えるため分割内容を確認してください',))
    tasks: dict[str, ActionTask] = {}
    conflicted: set[str] = set()
    holds: list[str] = []
    for item in items:
        if not item or item in _NONE:
            continue
        if re.match(r'^\[[xX]\]', item):
            holds.append('完了チェック付きのアクションは自動登録しません')
            continue
        explicit = _ID.match(item)
        title = item[explicit.end():].strip() if explicit else item
        if not title or len(title) > 2000:
            holds.append('タスク本文が空または2000文字超のため登録を保留しました')
            continue
        normalized = unicodedata.normalize('NFKC', title)
        dates = _DATE.findall(normalized)
        identity = ' '.join(_DATE_WITH_WEEKDAY.sub('', normalized).split()).casefold().strip(' :：,、;；-')
        if not identity:
            holds.append('日付だけの項目はアクション本文が必要です')
            continue
        key = 'id-' + explicit.group(1) if explicit else 'auto-' + hashlib.sha256(identity.encode()).hexdigest()
        try:
            # 明示箇条書きは1件になっても項目内日付を維持。自然文1件だけは旧AI日を使う。
            raw_date = dates if _BULLET.search(action) else properties.get('AI推奨アクション日')
            due = select_action_date(raw_date, today=today)
        except (ValueError, TypeError):
            holds.append(f'タスク {key} の年月日が不正です')
            continue
        task = ActionTask(key, title, due.isoformat() if due else None, project_id,
                          str(properties.get('案件名') or '（案件名未設定）'), source,
                          tuple(properties.get('担当メンバー') or ()))
        if key in tasks and tasks[key] != task:
            conflicted.add(key)
            holds.append(f'タスク {key} の識別子が重複しています。各項目に別の [id:識別子] を指定してください')
        else:
            tasks[key] = task
    return TaskPlan(tuple(task for key, task in tasks.items() if key not in conflicted), tuple(holds))
