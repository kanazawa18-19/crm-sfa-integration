"""外部接続なしで所属の確定条件を判定する。"""
from datetime import datetime, timezone, timedelta
from src.email_review.domain import normalized


def decide(contact, evidence, masters, now=None):
    now = now or datetime.now(timezone.utc)
    held = lambda reason: {'state': 'held', 'reason': reason, 'evidence': evidence}
    if contact.get('取引先マスター'):
        return held('既存所属あり')
    email = str(contact.get('メールアドレス') or '').strip().casefold()
    if not email or contact.get('人事異動フラグ'):
        return held('メール不足または人事異動')
    usable = []
    for row in evidence:
        if row.get('check_only'):
            continue
        try:
            stamp = datetime.fromisoformat(row['observed_at'].replace('Z', '+00:00'))
            if stamp.tzinfo is None or not now - timedelta(days=365) <= stamp <= now:
                continue
        except (KeyError, ValueError, TypeError, AttributeError):
            continue
        if (row.get('email', '').casefold() == email and row.get('origin')
                and row.get('source_id') and row.get('explicit') is True):
            usable.append(row)
    if any(e.get('source') == 'field25' and (not e.get('lookup_id') or not e.get('company_id')) for e in evidence):
        return held('Zohoの所属IDを既存取引先へ解決できない')
    checks = [e for e in evidence if e.get('check_only') and e.get('email', '').casefold() == email]
    names = {normalized(e['company_name']) for e in usable + checks}
    if len(names) != 1:
        return held('所属根拠が不足または矛盾')
    candidates = {m['id']: m for m in masters if normalized(m['name']) in names}
    if len(candidates) != 1:
        return held('取引先候補を一意に確定できない')
    # 同一原本のコピー・同じ送信者の繰り返しは増票しない。
    if len({e['origin'] for e in usable}) < 2:
        return held('独立した原本が2つ未満')
    company = next(iter(candidates.values()))
    if any(e.get('company_id') and e['company_id'] != company['id'] for e in evidence):
        return held('Zohoの所属IDと取引先候補が一致しない')
    return {'state': 'ready', 'reason': '独立した所属根拠が一致', 'company_id': company['id'],
            'company_name': company['name'], 'email': email, 'evidence': usable}
