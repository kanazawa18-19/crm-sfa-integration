"""アクション専用の実担当者と補足。代替アカウントへの割当はしない。"""
import json
import os
import re
from uuid import UUID
from src.hub_creation.domain import CreationHeld
from src.sync_engine.owner_mapping import owner_directory

BEGIN = '【CRM同期:実担当者】'
END = '【/CRM同期:実担当者】'


def _comment_parts(value):
    value = '' if value is None else value
    if not isinstance(value, str):
        raise ValueError('履歴メモの形式を確認してください')
    if BEGIN not in value and END not in value:
        return value, '', ''
    if value.count(BEGIN) != 1 or value.count(END) != 1:
        raise ValueError('実担当者の補足枠が重複または破損しています')
    start, finish = value.index(BEGIN), value.index(END)
    if finish < start or finish + len(END) != len(value):
        raise ValueError('実担当者の補足枠が破損しているか、末尾以外にあります')
    block = value[start:finish + len(END)]
    match = re.fullmatch(re.escape(BEGIN) + r'(\r?\n)実担当者: ([^\r\n]+)\1' + re.escape(END), block)
    if match is None:
        raise ValueError('実担当者の補足枠が破損しています')
    if not match.group(2).strip():
        raise ValueError('実担当者の補足枠が空です')
    prefix = value[:start]
    separator = next((sep for sep in ('\r\n\r\n', '\n\n') if prefix.endswith(sep)), '')
    if separator:
        prefix = prefix[:-len(separator)]
    return prefix, block, separator


def split_comment(value):
    body, block, _ = _comment_parts(value)
    return body, block


def preserve_comment(existing, incoming):
    _, block, separator = _comment_parts(existing)
    body, incoming_block = split_comment(incoming)
    # 外部から折り返された枠で、確定済みの実担当者を置き換えない。
    if incoming_block and incoming_block != block:
        raise ValueError('実担当者の補足枠は履歴本文から変更できません')
    if body and block and not separator:
        separator = '\r\n\r\n' if '\r\n' in block else '\n\n'
    result = body + (separator if body and block else '') + block
    if len(result) > 65535:
        raise ValueError('履歴メモの文字数上限を超えます')
    return result


def creation_comment_body(value):
    try:
        body, block = split_comment(value)
    except ValueError as exc:
        raise CreationHeld(str(exc)) from None
    if block:
        raise CreationHeld('新規作成の履歴本文に実担当者の補足枠が含まれています')
    return body


def comment_with_actor(value, names):
    body = creation_comment_body(value)
    if any(not isinstance(name, str) or not name.strip() or '\n' in name or '\r' in name
           or '【' in name or '】' in name for name in names):
        raise CreationHeld('実担当者の表示名を確認してください')
    block = BEGIN + '\n実担当者: ' + '、'.join(names) + '\n' + END
    result = body + ('\n\n' if body else '') + block
    if len(result) > 65535:
        raise CreationHeld('履歴メモの文字数上限を超えます')
    return result


def action_actors(properties, context):
    ids = properties.get('担当営業')
    if not isinstance(ids, list) or any(not isinstance(v, str) for v in ids):
        raise CreationHeld('担当営業の集計が未確定です。関連先と集計完了を確認してください')
    if not ids:
        try:
            data = json.loads(os.environ.get('CRM_ACTION_ACTORS_JSON', '{}'))
            page_id = str(UUID(context['sourceId']))
            override = data['records'][page_id]
            if data.get('verified') is not True or str(UUID(override['expected_created_by'])) != str(UUID(context['action_created_by'])):
                raise ValueError
            ids = [str(UUID(override['notion_user_id']))]
        except (ValueError, KeyError, TypeError, AttributeError):
            raise CreationHeld('実担当者が未確定です。原本ごとの確認済み担当者設定が必要です') from None
    directory = owner_directory()
    if directory is None:
        raise CreationHeld('担当者の確認済み対応設定がありません')
    by_id = {row['notion_id']: row for row in directory['users']}
    rows = [by_id.get(identifier) for identifier in dict.fromkeys(ids)]
    if any(row is None for row in rows):
        raise CreationHeld('実担当者の確認済み対応設定がありません')
    codes = [row.get('kintone_code') for row in rows]
    # 複数担当の一部だけをアカウント欄へ残して、残りを担当外に見せない。
    if all(codes):
        return [{'code': code} for code in codes], []
    names = [row.get('display_name') for row in rows]
    if any(not isinstance(name, str) or not name.strip() for name in names):
        raise CreationHeld('実担当者の表示名が未設定です')
    return [], names


def action_service(properties, fields):
    services = properties.get('提案サービス')
    if not isinstance(services, list) or any(not isinstance(v, str) for v in services):
        raise CreationHeld('提案サービスの集計が未確定です')
    if services:
        return services
    if '未確定' not in fields.get('service', {}).get('options', {}):
        raise CreationHeld('提案サービスの未確定の選択肢を確認できません')
    return ['未確定']
