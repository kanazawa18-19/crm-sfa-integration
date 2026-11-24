"""外部通信を行わず、案件から終日予定の登録内容を組み立てる。"""

from __future__ import annotations

from datetime import date, datetime
import re
from typing import Any, Mapping
from uuid import UUID
from zoneinfo import ZoneInfo


CALENDAR_FIELDS = frozenset({
    "案件名", "次回アクション", "AI推奨アクション", "AI推奨アクション日", "担当メンバー",
})


def calendar_properties(raw: Mapping[str, Any]) -> dict[str, Any]:
    """カレンダーに必要な項目だけを読む。他ツールの同期項目へは混ぜない。"""
    result: dict[str, Any] = {}
    for name in CALENDAR_FIELDS:
        prop = raw.get(name)
        if not isinstance(prop, Mapping):
            continue
        kind = prop.get("type")
        if kind in ("title", "rich_text"):
            result[name] = "".join(
                part.get("plain_text", part.get("text", {}).get("content", ""))
                for part in prop.get(kind, [])
            )
        elif kind == "formula" and name in {"次回アクション", "AI推奨アクション"}:
            formula = prop.get("formula") or {}
            if formula.get("type") == "string":
                result[name] = formula.get("string")
        elif kind == "date":
            dates = prop.get("date") or {}
            result[name] = [v for v in (dates.get("start"), dates.get("end")) if v]
        elif kind == "people":
            result[name] = [person["id"] for person in prop.get("people", [])]
    return result


def select_action_date(value: Any, *, today: date | None = None) -> date | None:
    """明示された候補から今日以降の最短日、全て過去なら最後の日を選ぶ。"""
    if not value:
        return None
    if isinstance(value, list):
        candidates = [select_action_date(item, today=today) for item in value]
        days = [day for day in candidates if day is not None]
    elif isinstance(value, str):
        # 候補全体を検証し、不正な日時の先頭10文字だけを採用しない。
        tokens = re.split(r"\s*(?:、|,|;|\n)\s*|\s+/\s+", value.strip())
        days = []
        for token in tokens:
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", token):
                days.append(date.fromisoformat(token))
            elif re.fullmatch(r"\d{4}-\d{2}-\d{2}T.+", token):
                days.append(datetime.fromisoformat(token.replace("Z", "+00:00")).date())
            else:
                match = re.fullmatch(r"(\d{4})/(\d{1,2})/(\d{1,2})", token)
                if match is None:
                    match = re.fullmatch(r"(\d{4})年(\d{1,2})月(\d{1,2})日", token)
                if match is None:
                    raise ValueError("AI推奨アクション日は明示された年月日で指定してください")
                days.append(date(*(int(part) for part in match.groups())))
    else:
        raise ValueError("AI推奨アクション日は日付文字列で指定してください")
    reference = today or datetime.now(ZoneInfo("Asia/Tokyo")).date()
    future = [day for day in days if day >= reference]
    return min(future) if future else max(days) if days else None


def build_event_plan(properties: Mapping[str, Any], notion_page_id: str) -> dict[str, str] | None:
    """日付なし・内容なしは登録しない。日時指定も既存APIに合わせ終日とする。"""
    action = ""
    source = ""
    for name in ("次回アクション", "AI推奨アクション"):
        value = properties.get(name)
        if isinstance(value, str) and value.strip():
            if value.strip() in {"なし", "アクションなし", "対応不要", "該当なし", "未入力", "-", "—"}:
                return None
            action, source = value.strip(), name
            break
    raw_date = properties.get("AI推奨アクション日")
    if not action or not raw_date:
        return None
    day = select_action_date(raw_date)
    if day is None:
        return None
    page_id = str(UUID(notion_page_id))
    project = str(properties.get("案件名") or "（案件名未設定）").strip()
    short_action = " ".join(action.split())
    if len(short_action) > 100:
        short_action = short_action[:100] + "…"
    return {
        "notion_project_id": page_id,
        "summary": f"{project} - {short_action}",
        "date": day.isoformat(),
        "description": f"{source}\n{action}\n\nNotion案件: https://www.notion.so/{UUID(page_id).hex}",
    }
