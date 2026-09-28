"""確認済みの担当者対応だけを使う。実ユーザーIDは環境設定に置く。"""
from __future__ import annotations
import json
import os

OWNER_PROPERTIES = {'project': '担当メンバー', 'contact': '担当メンバー', 'chain': '担当'}
UNRESOLVED_OWNER = object()


def owner_directory():
    raw = os.environ.get('CRM_USER_MAPPING_JSON')
    if not raw:
        return None
    try:
        data = json.loads(raw)
        if data.get('verified') is not True or not isinstance(data.get('users'), list):
            return None
        users = data['users']
        for key in ('notion_id', 'zoho_id', 'kintone_code'):
            values = [row[key] for row in users if row.get(key)]
            if any(not isinstance(value, str) for value in values) or len(set(values)) != len(values):
                return None
        if any(not isinstance(row.get('notion_id'), str) or not row['notion_id'] or row.get('enabled') is not True for row in users):
            return None
        return data
    except (ValueError, TypeError, AttributeError, KeyError):
        return None


def owner_to_external(ids, tool, *, required=False):
    """空欄不可の送り先だけ、設定で確認済みの本人へ対応させる。"""
    data = owner_directory()
    if data is None or not isinstance(ids, (list, tuple)) or any(not isinstance(value, str) for value in ids):
        return UNRESOLVED_OWNER
    key = {'zoho': 'zoho_id', 'kintone': 'kintone_code'}[tool]
    by_notion = {row['notion_id']: row for row in data['users']}
    values = [by_notion.get(identifier, {}).get(key) for identifier in dict.fromkeys(ids)]
    # 複数担当を単一Ownerへ縮めない。一部だけ対応する場合も勝手に減らさない。
    if len(values) > 1 and (tool == 'zoho' or any(value is None for value in values)):
        return UNRESOLVED_OWNER
    values = [value for value in values if value]
    if not values and required:
        fallback = by_notion.get(data.get('fallback_notion_id'), {}).get(key)
        if not fallback:
            return UNRESOLVED_OWNER
        values = [fallback]
    return ({'id': values[0]} if values else None) if tool == 'zoho' else [{'code': value} for value in values]


def owner_from_external(value, tool):
    data = owner_directory()
    if data is None:
        return UNRESOLVED_OWNER
    if value in (None, '', []):
        return []
    values = [value] if tool == 'zoho' else value
    key, source_key = ('zoho_id', 'id') if tool == 'zoho' else ('kintone_code', 'code')
    if not isinstance(values, list) or any(not isinstance(item, dict) or not isinstance(item.get(source_key), str) for item in values):
        return UNRESOLVED_OWNER
    reverse = {row[key]: row['notion_id'] for row in data['users'] if row.get(key)}
    result = [reverse.get(item[source_key]) for item in values]
    return UNRESOLVED_OWNER if any(value is None for value in result) else list(dict.fromkeys(result))


def owner_payload(db_key, properties, tool, *, required=False):
    name = OWNER_PROPERTIES.get(db_key)
    field = 'Owner' if tool == 'zoho' else '営業担当者' if db_key == 'project' else None
    if not name or not field or name not in properties:
        return {}, set()
    value = owner_to_external(properties[name], tool, required=required)
    if value is UNRESOLVED_OWNER:
        return {}, set()
    return {field: value}, {name}


def owner_native_field(tool, db_key, name):
    if OWNER_PROPERTIES.get(db_key) != name:
        return None
    return 'Owner' if tool == 'zoho' else '営業担当者' if tool == 'kintone' and db_key == 'project' else None


def hold_owner_inbound(current, incoming, tool):
    """単一欄への縮約と必須代替の折返しから、Notion側の担当を保護する。"""
    if not isinstance(current, (list, tuple)) or not isinstance(incoming, (list, tuple)):
        return True
    if set(current) == set(incoming):
        return False
    if tool == 'zoho' and len(set(current)) > 1:
        return True
    data = owner_directory()
    if data is None:
        return True
    fallback = data.get('fallback_notion_id')
    if fallback and list(incoming) == [fallback]:
        external = owner_to_external(current, tool)
        if external is UNRESOLVED_OWNER or external in (None, []):
            return True
    return False


def zoho_owner_required(client, module):
    """field/layoutの両方から必須を確認する。未取得は任意と推測しない。"""
    from urllib.parse import urlencode
    from src.sync_engine.clients._http import ApiError, raise_for_error
    def fetch(kind):
        response = client._request('GET', '/settings/' + kind + '?' + urlencode({'module': module}))
        raise_for_error(response, ApiError)
        value = response.json().get(kind)
        if not isinstance(value, list) or not value:
            raise ValueError('担当者の必須設定を確認できません')
        return value
    fields, layouts = fetch('fields'), fetch('layouts')
    owner = next((field for field in fields if field.get('api_name') == 'Owner'), None)
    if owner is None or any(not isinstance(layout.get('sections'), list) for layout in layouts):
        raise ValueError('担当者の必須設定を確認できません')
    return bool(owner.get('system_mandatory') or owner.get('required') or any(
        field.get('api_name') == 'Owner' and (field.get('required') or field.get('system_mandatory'))
        for layout in layouts for section in layout['sections'] for field in section.get('fields', [])))
