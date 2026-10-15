"""アクションはタイトルではなく、確定した相手と接触内容で比較する。"""
from datetime import date, datetime
import hashlib
import json
import unicodedata
from uuid import UUID
from src.hub_creation.domain import CreationHeld

VERSION = 'action-content-v1'
CLIENT = '👨‍👩‍👧‍👦 取引先マスター'
TEXT_FIELDS = ('アクション種別', '履歴メモ', '先方担当者')


def text(value):
    if value is None:
        return ''
    if not isinstance(value, str):
        raise CreationHeld('アクションの照合項目の形式を確認してください')
    return unicodedata.normalize('NFKC', value).strip()


def relation_ids(value):
    if value in (None, []):
        return []
    if not isinstance(value, (list, tuple)) or any(not isinstance(v, str) for v in value):
        raise CreationHeld('アクションの関連先IDを確認してください')
    try:
        return sorted(set(str(UUID(v)) for v in value))
    except ValueError:
        raise CreationHeld('アクションの関連先IDを確認してください') from None


def action_day(value):
    value = text(value)
    if not value:
        return ''
    try:
        # 日付欄の時刻指定も同日の接触として比較する。日付だけの外部欄と揃える。
        return (datetime.fromisoformat(value.replace('Z', '+00:00')).date().isoformat()
                if 'T' in value else date.fromisoformat(value).isoformat())
    except ValueError:
        raise CreationHeld('アクション日の形式を確認してください') from None


def validate_notion_identity(raw):
    """空欄と、API応答から欠けた欄を区別する。原本と候補の双方で使う。"""
    props = raw.get('properties')
    if not isinstance(props, dict):
        raise CreationHeld('アクションの照合項目が欠けています')
    types = {CLIENT: 'relation', '案件名': 'relation', 'アクション日': 'date',
             'アクション種別': 'select', '履歴メモ': 'rich_text', '先方担当者': 'rich_text'}
    for name, kind in types.items():
        prop = props.get(name)
        if not isinstance(prop, dict) or prop.get('type') != kind or kind not in prop:
            raise CreationHeld('アクションの照合項目が欠けているか、型が異なります')
        value = prop[kind]
        if prop.get('has_more'):
            raise CreationHeld('アクションの照合項目を最後まで確認できません')
        if kind == 'relation':
            if not isinstance(value, list) or any(not isinstance(v, dict) or not isinstance(v.get('id'), str) for v in value):
                raise CreationHeld('アクションの関連先IDを確認してください')
            relation_ids([v['id'] for v in value])
        elif kind == 'rich_text':
            if not isinstance(value, list) or any(not isinstance(v, dict) or not isinstance(v.get('plain_text'), str) for v in value):
                raise CreationHeld('アクションの文字項目を確認できません')
        elif kind == 'date':
            if value is not None:
                if not isinstance(value, dict) or not isinstance(value.get('start'), str) or not value['start']:
                    raise CreationHeld('アクション日の形式を確認してください')
                action_day(value['start'])
        elif value is not None and (not isinstance(value, dict) or not isinstance(value.get('name'), str)):
            raise CreationHeld('アクション種別の形式を確認してください')


def identity(properties):
    clients = relation_ids(properties.get(CLIENT))
    if len(clients) != 1:
        raise CreationHeld('アクションの取引先を1件に確定してください。案件なしでも登録できます')
    day = action_day(properties.get('アクション日'))
    return [VERSION, clients, day, *[text(properties.get(k)) for k in TEXT_FIELDS],
            relation_ids(properties.get('案件名'))]


def fingerprint(properties, *, external=False):
    # 外部には案件欄がないため、案件だけ違う同内容も同じ作成予約にする。
    if external:
        properties = {**properties, '案件名': []}
    value = json.dumps(identity(properties), ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(value.encode()).hexdigest()


def zoho_matcher(payload):
    # 案件はZohoへの対応欄がないため、共通する確定項目で保守的に比較する。
    fields = ['field6', 'field7', 'field4', 'field', 'field2']
    expected_client = payload.get('field6')
    expected_id = expected_client.get('id') if isinstance(expected_client, dict) else None
    if not isinstance(expected_id, str) or not expected_id:
        raise CreationHeld('アクションの取引先のZoho IDを確認してください')
    expected = [action_day(payload.get(k)) if k == 'field4' else text(payload.get(k)) for k in fields[1:]]
    def matches(record):
        if any(k not in record for k in fields):
            raise CreationHeld('外部アクションの照合項目が欠けています')
        client = record['field6']
        if client is None:
            return False
        if not isinstance(client, dict) or not isinstance(client.get('id'), str):
            raise CreationHeld('外部アクションの取引先IDを確認できません')
        return client['id'] == expected_id and [action_day(record[k]) if k == 'field4' else text(record[k]) for k in fields[1:]] == expected
    matches.comparison = {'fields': fields, 'values': [expected_id, *expected]}
    return fields, matches
