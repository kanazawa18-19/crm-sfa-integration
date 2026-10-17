"""既存連絡先の補完計画。外部I/Oを持たず、曖昧な候補は更新しない。"""
from __future__ import annotations

import re
from src.email_review.domain import normalized


def name_parts(properties: dict) -> dict:
    """判別できる氏名は分割し、不明なら名前全体を姓へ空欄補完する。"""
    parts = str(properties.get('名前') or '').split()
    if len(parts) != 2 or not all(re.fullmatch(r'[一-龯㐀-䶿豈-﫿\U00020000-\U0002EBEF々〆ヵヶぁ-ゖァ-ヺー]+', p) for p in parts):
        return {'姓': str(properties.get('名前') or '').strip()} if str(properties.get('名前') or '').strip() and not properties.get('姓') and not properties.get('名') else {}
    expected = dict(zip(('姓', '名'), parts))
    if any(properties.get(k) and normalized(properties[k]) != normalized(v) for k, v in expected.items()):
        return {}
    return {k: v for k, v in expected.items() if not properties.get(k)}


def contact_changes(current: dict, *, email: str, company_id: str, replace_email: bool) -> dict:
    """旧メール・既存備考を保存し、過去メールへの巻き戻しを拒否する。"""
    changes = name_parts(current)
    affiliations = set(current.get('取引先マスター') or [])
    if affiliations and affiliations != {company_id}:
        raise ValueError('既存所属が異なります')
    if not affiliations:
        changes['取引先マスター'] = [company_id]
    old = str(current.get('メールアドレス') or '').strip()
    email = email.strip().casefold()
    if old.casefold() != email:
        if not replace_email:
            raise ValueError('メールが変わっています')
        memo = str(current.get('備考') or '')
        history = re.findall(r'^過去メールアドレス[:：]\s*(\S+)\s*$', memo, re.M)
        if email in {v.casefold() for v in history}:
            raise ValueError('過去メールアドレスへの差替えは確認が必要です')
        changes['メールアドレス'] = email
        if old:
            if not current.get('_memo_plain', True):
                raise ValueError('備考に書式・リンクがあるため、メール変更は手動確認してください')
            line = '過去メールアドレス: ' + old
            changes['備考'] = memo + ('\n' if memo and not memo.endswith('\n') else '') + line
    return changes


def unique_named_contact(contacts: list[dict], names: list[str]) -> dict | None:
    """会社所属内で、宛名と完全一致または明示された姓に一致する人を1件に絞る。"""
    wanted = {normalized(re.sub(r'(?:様|さん|殿)[、。\s]*$', '', n).strip()) for n in names if n.strip()}
    if len(wanted) != 1:
        return None
    hits = {}
    for contact in contacts:
        full = str(contact.get('名前') or '')
        surname = str(contact.get('姓') or '')
        if not surname:
            surname = name_parts({'名前': full}).get('姓', '')
        if surname == full.strip() and normalized(full) not in wanted and any(normalized(full).startswith(n) for n in wanted):
            # 姓を分割できない別人が同姓の可能性を残す場合、一意とは扱わない。
            return None
        if normalized(full) in wanted or surname and normalized(surname) in wanted:
            hits[contact['id']] = contact
    return next(iter(hits.values())) if len(hits) == 1 else None
