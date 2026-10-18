"""姓名補完の判断。DBや外部APIに依存しない。"""
import hashlib
import json
from uuid import UUID

from src.email_review.contact_completion import name_parts

JOB_ID = 'contact-names-20261017'
FIELDS = ('名前', '姓', '名')


def matches(actual, expected):
    return all((actual.get(k) or '') == (expected.get(k) or '') for k in FIELDS)


def decision(entry, current):
    if matches(current, {**entry['before'], **entry['changes']}):
        return 'verified'
    if not matches(current, entry['before']) or name_parts(current) != entry['changes']:
        return 'held'
    return 'update'


def validate_plan(entries):
    seen = set()
    for entry in entries:
        key = str(UUID(entry['id']))
        if key != entry['id'] or key in seen:
            raise ValueError('IDが不正または重複しています')
        seen.add(key)
        before, changes = entry['before'], entry['changes']
        if set(before) - set(FIELDS) or not changes or set(changes) - {'姓', '名'}:
            raise ValueError('姓名以外の変更は許可していません')
        if any(v is not None and not isinstance(v, str) for v in before.values()) or any(not isinstance(v, str) for v in changes.values()):
            raise ValueError('文字列以外の計画です')
        if name_parts(before) != changes:
            raise ValueError('承認済みの分割規則と一致しません')
    return hashlib.sha256(json.dumps(entries, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
