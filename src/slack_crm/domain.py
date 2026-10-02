"""Slack画面から許す更新内容。外部APIやDBに依存しない。"""
from __future__ import annotations

from datetime import date
from typing import Any

from src.db_schema.project import ACTIVE_STATUSES, CONFIDENCE_LEVELS
from src.db_schema.action import ACTION_SCHEMA


PROJECT_FIELDS = frozenset({"営業ステータス", "次回アクション日", "確度"})
ACTION_FIELDS = frozenset({"アクション種別", "アクション日", "履歴メモ"})
ACTION_TYPES = frozenset(
    next(p.options for p in ACTION_SCHEMA.properties if p.name == "アクション種別")
)


def validate_changes(kind: str, changes: dict[str, Any]) -> dict[str, Any]:
    """許可項目だけを受け、値の形式と長さを確定する。"""
    if kind == "project_update":
        allowed = PROJECT_FIELDS
    elif kind == "action_update":
        allowed = ACTION_FIELDS
    elif kind == "action_create":
        allowed = frozenset({"商談回数・電話回数・メール回数（何回目）", "アクション種別", "アクション日", "履歴メモ"})
    else:
        raise ValueError("操作種別が不正です")
    if not changes or set(changes) - allowed:
        raise ValueError("更新できない項目が含まれています")
    if kind == "action_create" and (not {"商談回数・電話回数・メール回数（何回目）", "アクション種別", "アクション日"} <= set(changes)
                                    or not changes.get("アクション日")):
        raise ValueError("アクションの必須項目が不足しています")
    result: dict[str, Any] = {}
    for key, value in changes.items():
        if key == "営業ステータス":
            if value not in ACTIVE_STATUSES:
                raise ValueError("この営業ステータスはSlackから変更できません")
        elif key == "確度":
            if value not in CONFIDENCE_LEVELS:
                raise ValueError("確度が不正です")
        elif key in {"次回アクション日", "アクション日"}:
            if value is None:
                result[key] = None
                continue
            if not isinstance(value, str):
                raise ValueError("日付が不正です")
            try:
                date.fromisoformat(value)
            except ValueError as exc:
                raise ValueError("日付が不正です") from exc
        elif key == "アクション種別":
            if value not in ACTION_TYPES:
                raise ValueError("アクション種別が不正です")
        elif key == "履歴メモ":
            if not isinstance(value, str) or len(value) > 500:
                raise ValueError("履歴メモは500文字以内です")
            value = value.strip()
        elif key == "商談回数・電話回数・メール回数（何回目）":
            if not isinstance(value, str) or not (1 <= len(value.strip()) <= 120):
                raise ValueError("アクション名は1〜120文字です")
            value = value.strip()
        result[key] = value
    return result


def changed_fields(current: dict[str, Any], proposed: dict[str, Any]) -> dict[str, Any]:
    """表示上の同値を除き、実際の差分だけを残す。"""
    return {key: value for key, value in proposed.items() if current.get(key) != value}
