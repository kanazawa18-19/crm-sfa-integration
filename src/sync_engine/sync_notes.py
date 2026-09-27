"""未同期項目の判断メモ。外部の任意の生値は本文へ転記しない。"""
from __future__ import annotations

import re
from typing import Any, Mapping

from src.db_schema.base import Tool
from src.sync_engine.known_sync_gaps import KNOWN_UNMAPPED_FIELDS, KNOWN_UNMAPPED_RELATIONS

MARKER = "CRM同期メモ（自動更新 / crm-sync-notes-v1）"
CHOICES = "選択肢・担当者・関連付け"
UNAVAILABLE = "APIで取得不能・曖昧な名前・対応項目なし"


def safe_business_value(value: Any) -> str:
    """既知の業務項目だけに使用。人・関連先は表示名だけを取り出す。"""
    if isinstance(value, dict):
        value = value.get("name", "（表示名を取得できません）")
    if isinstance(value, list):
        value = "、".join(safe_business_value(item) for item in value[:10])
    if not isinstance(value, (str, int, float, bool)):
        return "（表示できない形式）"
    text = str(value).replace("\n", " ").replace("\r", " ")
    if re.search(r"(?i)(xox[baprs]-|sk-[a-z0-9]|bearer\s|(?:api[_ -]?key|token|secret|password)\s*[:=]|-----BEGIN|eyJ[a-zA-Z0-9_-]{15,}\.)", text):
        return "（認証情報の可能性があるため省略）"
    return text[:300] + ("…（省略）" if len(text) > 300 else "")


def note_entry(tool: Tool, name: str, reason: str, *, has_value: bool = False, value: Any = None) -> str:
    state = "送信元に値あり。送信元の値と対応先を確認してください" if has_value else "値の有無は未確認。必要な場合は送信元の項目と対応先を確認してください"
    detail = f" 元の値: {safe_business_value(value)}。" if has_value and value is not None else ""
    return f"[{tool.value}:{name}] {reason}。{state}。{detail}既存の値は保持します。"


def gap_notes(tool: Tool, db_key: str, values_by_name: Mapping[str, Any]) -> dict[str, str]:
    """既知の未対応項目だけを扱う。未知のキーや値をメモへ流さない。"""
    notes = {}
    for (source, db, name), reason in {**KNOWN_UNMAPPED_FIELDS, **KNOWN_UNMAPPED_RELATIONS}.items():
        if source != tool or db != db_key:
            continue
        if name == "確度（数値）" and tool == Tool.KINTONE:
            continue  # 本人指定: kintoneに無ければ無視する。
        present = name in values_by_name and values_by_name[name] not in (None, "", [], {})
        choice = any(word in reason for word in ("選択", "担当者", "真偽値"))
        if choice and not present:
            if name in values_by_name:
                notes[f"{CHOICES}|{tool.value}:{name}"] = ""
            continue
        category = CHOICES if choice else UNAVAILABLE
        if name in values_by_name and not present:
            notes[f"{category}|{tool.value}:{name}"] = ""
            continue
        explanation = (
            "選択肢・人物・関連先の対応が未確定" if choice
            else "APIの応答に含まれないため取得できません" if "APIの応答に含まれない" in reason
            else "対応する項目、または名前による関連先が未確定"
        )
        notes[f"{category}|{tool.value}:{name}"] = note_entry(tool, name, explanation, has_value=present, value=values_by_name.get(name))
    return notes


def unresolved_note(tool: Tool, name: str, value: Any = None) -> tuple[str, str]:
    return (f"{CHOICES}|{tool.value}:{name}", note_entry(tool, name, "値を安全に変換・関連付けできませんでした", has_value=True, value=value))


def render_notes(notes: Mapping[str, str]) -> str:
    sections = [MARKER, "この枠は自動更新されます。判断内容は枠の外に記載してください。"]
    for category in (CHOICES, UNAVAILABLE):
        sections.append("\n" + category)
        entries = [value for key, value in sorted(notes.items()) if key.startswith(category + "|")]
        sections.extend(entries or ["該当する未解決値は検出されていません。"])
    return "\n".join(sections)


def parse_notes(text: str) -> dict[str, str]:
    result = {}
    category = None
    for line in text.splitlines():
        if line in (CHOICES, UNAVAILABLE):
            category = line
        elif category and line.startswith(("[zoho:", "[kintone:", "[notion:", "[spreadsheet:")) and "] " in line:
            key = line[1:line.index("] ")]
            result[f"{category}|{key}"] = line
    return result


def resolved_notes(tool: Tool, name: str) -> dict[str, str]:
    return {f"{category}|{tool.value}:{name}": "" for category in (CHOICES, UNAVAILABLE)}


def merge_notes(existing: Mapping[str, str], incoming: Mapping[str, str]) -> dict[str, str]:
    """部分通知の未送信値で、前回の業務値を消さない。初期説明は不足分だけ補う。"""
    result = dict(existing)
    for key, value in incoming.items():
        if key in result and "値の有無は未確認。" in value:
            continue
        if value:
            result[key] = value
        else:
            result.pop(key, None)
    return result
