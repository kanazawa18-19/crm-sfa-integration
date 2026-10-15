"""アクション専用の実担当者と補足。代替アカウントへの割当はしない。"""
import json
import os
import re
from uuid import UUID
from src.hub_creation.domain import CreationHeld
from src.sync_engine.owner_mapping import owner_directory

BEGIN = '【CRM同期:実担当者】'
END = '【/CRM同期:実担当者】'


def _valid_actor_name(name):
    return (isinstance(name, str) and bool(name.strip())
            and name.splitlines() == [name] and '【' not in name and '】' not in name)


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
    if any(not _valid_actor_name(name) for name in names):
        raise CreationHeld('実担当者の表示名を確認してください')
    block = BEGIN + '\n実担当者: ' + '、'.join(names) + '\n' + END
    result = body + ('\n\n' if body else '') + block
    if len(result) > 65535:
        raise CreationHeld('履歴メモの文字数上限を超えます')
    return result


def action_actor_rows(properties, context):
    ids = properties.get('担当営業')
    if not isinstance(ids, list) or any(not isinstance(v, str) for v in ids):
        raise CreationHeld('担当営業の集計が未確定です。関連先と集計完了を確認してください')
    if not ids:
        try:
            data = json.loads(os.environ.get('CRM_ACTION_ACTORS_JSON', '{}'))
            page_id = str(UUID(context['sourceId']))
            creator = str(UUID(context['action_created_by']))
            if data.get('verified') is not True:
                raise ValueError
            raw_records = data.get('records', {})
            if not isinstance(raw_records, dict):
                raise ValueError
            records = {}
            for key, value in raw_records.items():
                canonical = str(UUID(key))
                if canonical in records:
                    raise ValueError
                records[canonical] = value
            if page_id in records:
                # 原本別の設定がある場合は、作成者承認で照合失敗を救済しない。
                override = records[page_id]
                if str(UUID(override['expected_created_by'])) != creator:
                    raise ValueError
                ids = [str(UUID(override['notion_user_id']))]
            else:
                approved = data.get('self_actor_creators', [])
                if not isinstance(approved, list) or not approved:
                    raise ValueError
                approved = [str(UUID(value)) for value in approved]
                if creator not in approved:
                    raise ValueError
                ids = [creator]
        except (ValueError, KeyError, TypeError, AttributeError):
            raise CreationHeld('実担当者が未確定です。原本別設定または作成者本人が担当する承認済み設定が必要です') from None
    directory = owner_directory()
    if directory is None:
        raise CreationHeld('担当者の確認済み対応設定がありません')
    try:
        ids = list(dict.fromkeys(str(UUID(identifier)) for identifier in ids))
    except (ValueError, TypeError, AttributeError):
        raise CreationHeld('実担当者のユーザーIDを確認してください') from None
    if len(ids) > 100:
        raise CreationHeld('実担当者が100名を超えています')
    by_id = {}
    try:
        for row in directory['users']:
            identifier = str(UUID(row['notion_id']))
            if identifier in by_id:
                raise ValueError
            by_id[identifier] = {**row, 'notion_id': identifier}
    except (ValueError, TypeError, AttributeError):
        raise CreationHeld('担当者対応のユーザーIDが不正または重複しています。設定を確認してください') from None
    return [by_id.get(identifier, {'notion_id': identifier}) for identifier in ids]


def action_profile_ids(properties, context):
    """氏名の確認が必要なIDだけを返す。取得は外部アダプターが担当する。"""
    rows = action_actor_rows(properties, context)
    if all(row.get('kintone_code') for row in rows):
        return []
    return [row['notion_id'] for row in rows if not row.get('display_name')]


def action_actors(properties, context, *, profiles=None):
    rows = action_actor_rows(properties, context)
    codes = [row.get('kintone_code') for row in rows]
    # 複数担当の一部だけをアカウント欄へ残して、残りを担当外に見せない。
    if all(codes):
        return [{'code': code} for code in codes], []
    names = []
    for row in rows:
        name = row.get('display_name')
        if not name:
            profile = (profiles or {}).get(row['notion_id'])
            try:
                if (not isinstance(profile, dict) or profile.get('object') != 'user'
                        or profile.get('type') != 'person'
                        or str(UUID(profile['id'])) != row['notion_id']):
                    raise ValueError
                name = profile.get('name')
            except (ValueError, KeyError, TypeError, AttributeError):
                raise CreationHeld(f'実担当者の氏名をNotionで確認できません（ユーザーID: {row["notion_id"]}）。人間のユーザー・氏名・連携権限を確認し、解消後にこの原本の同期を再実行してください') from None
        # 空欄・bot・別ID・管理枠を壊す氏名は書き込まない。
        if not _valid_actor_name(name):
            raise CreationHeld(f'実担当者の表示名が無効です（ユーザーID: {row["notion_id"]}）。空欄・改行・【】を含まない氏名へ修正し、この原本の同期を再実行してください')
        names.append(name)
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
