"""作成可否の判断。DBと外部APIを使わず検証できる。"""
from __future__ import annotations
import hashlib
import unicodedata
from typing import Any, Mapping
from src.db_schema.base import PropertyType
from src.db_schema.registry import get_schema


class CreationHeld(ValueError):
    """条件を満たさず、新規作成を保留する。理由は固定文のみ。"""


class CreationNotApplicable(ValueError):
    """対応先が存在しないため、作成も再試行も不要。"""


def title_value(db_key: str, properties: Mapping[str, Any]) -> tuple[str, str]:
    schema = get_schema(db_key)
    name = next(p.name for p in schema.properties if p.property_type == PropertyType.TITLE)
    value = properties.get(name)
    if not isinstance(value, str) or not value.strip():
        raise CreationHeld("名前が未入力です")
    title = value.strip()
    if title.startswith(("CRM同期確認_", "CRM同期テスト_")):
        raise CreationHeld("隔離テスト用のため他ツールへ新規作成しません")
    return name, title


def identity_hash(db_key: str, title: str) -> str:
    normalized = unicodedata.normalize("NFKC", title).casefold().strip()
    return hashlib.sha256(f"{db_key}:{normalized}".encode()).hexdigest()


def require_notion_fields(db_key: str, properties: Mapping[str, Any]) -> None:
    title_value(db_key, properties)
    missing = [p.name for p in get_schema(db_key).properties if p.is_required and properties.get(p.name) in (None, "", [], {})]
    if missing:
        raise CreationHeld("必須項目が未入力です: " + "・".join(missing))


def sheet_properties(db_key: str, values: Mapping[str, Any]) -> dict[str, Any]:
    """新規シート登録の値だけを型変換。曖昧なリレーション・担当者は作成しない。"""
    import math
    from datetime import date, timedelta
    result = {}
    for prop in get_schema(db_key).properties:
        value = values.get(prop.name)
        if not prop.is_writable or value in (None, ""):
            continue
        kind = prop.property_type
        if kind in (PropertyType.TITLE, PropertyType.TEXT, PropertyType.EMAIL, PropertyType.PHONE, PropertyType.URL):
            if db_key == "chain" and prop.name == "施設数" and not isinstance(value, str):
                # Sheetsの非書式値では、文字列欄の施設数も数値として返る。
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise CreationHeld("施設数は非負の整数で入力してください")
                if value < 0 or isinstance(value, float) and (not math.isfinite(value) or not value.is_integer()):
                    raise CreationHeld("施設数は非負の整数で入力してください")
                value = str(int(value))
            if not isinstance(value, str):
                raise CreationHeld("文字の形式を確認してください: " + prop.name)
        elif kind in (PropertyType.NUMBER, PropertyType.CURRENCY):
            if isinstance(value, bool):
                raise CreationHeld("数値の形式を確認してください: " + prop.name)
            try:
                value = float(value)
            except (ValueError, TypeError):
                raise CreationHeld("数値の形式を確認してください: " + prop.name) from None
            if not math.isfinite(value):
                raise CreationHeld("数値の形式を確認してください: " + prop.name)
        elif kind in (PropertyType.SELECT, PropertyType.STATUS):
            if value not in prop.options:
                raise CreationHeld("選択肢の対応確認が必要です: " + prop.name)
        elif kind == PropertyType.CHECKBOX and isinstance(value, bool):
            pass
        elif kind == PropertyType.DATE:
            try:
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    if not math.isfinite(value) or value != int(value):
                        raise ValueError
                    value = (date(1899, 12, 30) + timedelta(days=value)).isoformat()
                else:
                    value = date.fromisoformat(str(value).replace("/", "-")).isoformat()
            except (ValueError, OverflowError):
                raise CreationHeld("日付の形式を確認してください: " + prop.name) from None
        else:
            raise CreationHeld("担当者・関連付けなどの対応確認が必要です: " + prop.name)
        result[prop.name] = value
    require_notion_fields(db_key, result)
    return result
