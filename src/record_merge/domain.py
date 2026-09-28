"""統合内容の選択と再開可否。DB・外部APIには依存しない。"""
from __future__ import annotations

import copy
import hashlib
import json


class MergeHeld(ValueError):
    """内容の再確認または、保持方法の確認が必要。"""


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def choose_properties(source, target, choices, *, union_fields=()):
    """非空の相違は必ず人が選ぶ。空欄は補完し、関連は全件を保持する。"""
    result = {}
    conflicts = []
    unknown = set(choices) - (set(source) | set(target))
    if unknown:
        raise MergeHeld('比較対象にない項目が指定されています')
    for name in sorted(set(source) | set(target)):
        left, right = source.get(name), target.get(name)
        if name in union_fields:
            if not isinstance(left or [], list) or not isinstance(right or [], list):
                raise MergeHeld('関連・複数選択の形式を確認してください: ' + name)
            value = list(dict.fromkeys((right or []) + (left or [])))
            if len(value) > 100:
                raise MergeHeld('関連・複数選択が100件を超えています: ' + name)
        elif left == right or left in (None, '', [], {}):
            value = right
        elif right in (None, '', [], {}):
            value = left
        elif choices.get(name) == 'source':
            value = left
        elif choices.get(name) == 'target':
            value = right
        else:
            conflicts.append(name)
            continue
        result[name] = copy.deepcopy(value)
    if conflicts:
        raise MergeHeld('残す値を選んでください: ' + '、'.join(conflicts))
    return result


def replace_relation(ids, source_id, target_id):
    """元への関連だけを置換し、他の関連と順序を保持する。"""
    if not isinstance(ids, list) or any(not isinstance(item, str) for item in ids):
        raise MergeHeld('関連の全件を確認できません')
    result = list(dict.fromkeys(target_id if item == source_id else item for item in ids))
    if len(result) > 100:
        raise MergeHeld('関連が100件を超えるため個別確認が必要です')
    return result


_TEXT_BLOCKS = {'paragraph', 'heading_1', 'heading_2', 'heading_3', 'bulleted_list_item',
                'numbered_list_item', 'quote', 'toggle', 'to_do', 'callout', 'code'}


def copy_block(block):
    """期限付き添付や未知の型を見落として元を消さない。対応型だけ送信形式へ変換。"""
    kind = block.get('type')
    data = block.get(kind)
    if kind not in _TEXT_BLOCKS | {'divider', 'bookmark', 'embed', 'image', 'file', 'pdf', 'video', 'audio'} or not isinstance(data, dict):
        raise MergeHeld('本文に自動複製できない種類があります: ' + str(kind))
    content = {}
    if kind in _TEXT_BLOCKS:
        if data.get('caption'):
            raise MergeHeld('本文の説明文を個別に確認してください')
        content['rich_text'] = []
        for item in data.get('rich_text', []):
            item_kind = item.get('type')
            if item_kind not in {'text', 'equation'}:
                raise MergeHeld('本文のメンションは保持方法の確認が必要です')
            entry = {'type': item_kind, item_kind: copy.deepcopy(item[item_kind])}
            if 'annotations' in item:
                entry['annotations'] = copy.deepcopy(item['annotations'])
            content['rich_text'].append(entry)
        for name in ('color', 'checked', 'language'):
            if name in data:
                content[name] = data[name]
        if kind == 'callout':
            icon = data.get('icon')
            if icon and icon.get('type') != 'emoji':
                raise MergeHeld('本文アイコンの添付を保持できません')
            if icon:
                content['icon'] = copy.deepcopy(icon)
    elif kind in {'bookmark', 'embed'}:
        content['url'] = data['url']
        if data.get('caption'):
            raise MergeHeld('リンクの説明文を個別に確認してください')
    elif kind != 'divider':
        if data.get('type') != 'external' or not data.get('external', {}).get('url'):
            raise MergeHeld('Notion保管の添付は元を残して個別に保持してください')
        if data.get('caption'):
            raise MergeHeld('添付の説明文を個別に確認してください')
        content = {'type': 'external', 'external': copy.deepcopy(data['external'])}
    children = block.get('children', [])
    if block.get('has_children') and 'children' not in block:
        raise MergeHeld('本文の子ブロックを全件取得してください')
    if children:
        content['children'] = [copy_block(child) for child in children]
    return {'object': 'block', 'type': kind, kind: content}


def resumable_value(current, before, desired):
    """途中完了を再送せず、第三者の編集があれば停止する。"""
    if current == desired:
        return False
    if current != before:
        raise MergeHeld('確認後に値が変わりました。既存値を保持して再確認してください')
    return True


def block_count(blocks):
    """送信用の本文構造も子を含めて数える。"""
    return sum(1 + block_count(block.get(block.get('type'), {}).get('children', [])) for block in blocks)


def stable_blocks(blocks):
    """署名URLなど読取りごとの値を中止確認の版に含めない。"""
    result = []
    for block in blocks:
        try:
            result.append(copy_block(block))
        except (MergeHeld, KeyError, TypeError):
            result.append({'id': block.get('id'), 'type': block.get('type'),
                           'last_edited_time': block.get('last_edited_time'),
                           'children': stable_blocks(block.get('children', []))})
    return result
