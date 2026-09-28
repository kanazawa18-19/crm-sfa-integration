"""代表欄と全選択保存枠を、一つの承認対象として扱う。"""
from src.db_schema.base import Tool
from src.sync_review.domain import ReviewConflict


def storage_field(tool, db_key, name):
    if db_key != 'project':
        return None
    if tool is Tool.ZOHO and name == 'サイトコントローラー':
        return 'field70'
    if tool is Tool.KINTONE and name in {'サイトコントローラー', 'ファーストタッチ'}:
        return '文字列__複数行_'
    return None


def split_block(memo, name):
    memo = '' if memo is None else memo
    if not isinstance(memo, str):
        raise ReviewConflict('全選択保存枠の形式が不正です')
    begin, end = f'【CRM同期:{name}】', f'【/CRM同期:{name}】'
    if begin not in memo and end not in memo:
        return memo, ''
    if memo.count(begin) != 1 or memo.count(end) != 1 or memo.index(begin) > memo.index(end):
        raise ReviewConflict('全選択保存枠の形式が不正です')
    start, finish = memo.index(begin), memo.index(end) + len(end)
    prefix = memo[:start]
    if prefix.endswith('\n\n'):
        prefix = prefix[:-2]
    return prefix + memo[finish:], memo[start:finish]


def companion(tool, db_key, name, record):
    field = storage_field(tool, db_key, name)
    return split_block(record.get(field), name)[1] if field else None


VIRTUAL_CONTROLLER_FIELD = '_crm_controller_choices'


def enrich_record(tool, db_key, name, record):
    if tool is Tool.KINTONE and db_key == 'project' and name == 'サイトコントローラー':
        from src.sync_engine.decided_choices import choices, CONTROLLERS
        block = companion(tool, db_key, name, record)
        if block:
            value = choices(block.split('\n', 1)[1].rsplit('\n', 1)[0].split('、'), CONTROLLERS)
            if value is None:
                raise ReviewConflict('保存済みの選択値が不正です')
        else:
            value = []
        return {**record, VIRTUAL_CONTROLLER_FIELD: value}
    return record
