"""既存原本から所属だけを抽出する。本文やドメインの推測は票にしない。"""
import re
from datetime import datetime, timezone
from email.utils import getaddresses
from src.email_review.domain import evidence as extract, normalized, unquoted
from src.gmail_sync.mail_state import labels, sent


def addresses(message, key):
    return {address.strip().casefold() for _, address in getaddresses([message.get(key) or '']) if address}


def mail_evidence(rows, email, names=()):
    result = []
    for row in rows:
        message = row.get('message') or {}
        flags = labels(message.get('label_ids'))
        if flags is None or 'DRAFT' in flags or (row.get('correction_hold') or {}).get('active'):
            continue
        direction = row.get('direction')
        if direction == 'outbound':
            # 複数宛先は、冒頭の誰に向けた会社名か確定できない。
            if not sent(message) or addresses(message, 'to_header') != {email} or addresses(message, 'cc_header'):
                continue
            origin = 'mail:our-salutation'
        elif direction == 'inbound':
            if addresses(message, 'from_header') != {email}:
                continue
            origin = 'mail:counterpart-signature:' + email
        else:
            continue
        try:
            stamp = datetime.fromtimestamp(int(message['internal_date_ms']) / 1000, timezone.utc).isoformat()
        except (KeyError, ValueError, TypeError, OverflowError):
            continue
        entries = signature_evidence(message, email, names) if direction == 'inbound' else extract(message, direction, email)
        for entry in entries:
            result.append({'email': email, 'company_name': entry['value'], 'explicit': True,
                'origin': origin, 'source_id': 'email-review:' + row['id'], 'source': entry['source'],
                'observed_at': stamp, 'original_id': message.get('message_id_header') or message['id']})
    return result


def signature_evidence(message, email, names):
    """区切られた短い署名・本人氏名・メール完全一致がそろった場合だけ採る。"""
    lines = unquoted(message.get('body_text') or '')
    markers = [i for i, line in enumerate(lines) if re.fullmatch(r'[-_=━─＊*]{3,}', line)]
    expected_names = {normalized(n) for n in names if isinstance(n, str) and n.strip()}
    if not markers or not expected_names:
        return []
    for position in reversed(markers):
        end = next((i for i in markers if i > position), len(lines))
        block = lines[position + 1:end]
        if not block or len(block) > 15:
            continue
        # 紹介先・説明文・所属否定が混じる塊を署名と推定しない。
        if any(re.search(r'紹介|所属.{0,15}(?:ない|ません)|ではなく|代理|退職|異動|[。！？]|(?:です|ます|ません|ください)[、\s]*$', line) for line in block):
            continue
        tokens = {token.casefold() for line in block for token in
                  re.findall(r"[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-zA-Z0-9.-]+", line)}
        if tokens != {email} or not any(normalized(line) in expected_names for line in block):
            continue
        return [{'source': 'signature', 'value': line[:300], 'message_id': message['id']} for line in block]
    return []


def company_lines(rows, masters):
    # 署名の電話・氏名行を会社名の矛盾として扱わない。完全一致の社名行だけを使う。
    names = {normalized(m['name']) for m in masters}
    return [row for row in rows if normalized(row['company_name']) in names
            or re.search(r'株式会社|有限会社|合同会社|一般社団法人|ホテル|旅館|Hotel|Corporation|Inc\.', row['company_name'], re.I)]


def zoho_evidence(record, email, lookup_company_id=None):
    if str(record.get('Email') or '').strip().casefold() != email:
        return []
    result = []
    for field, name in [('field21', record.get('field21')),
                        ('field25', (record.get('field25') or {}).get('name'))]:
        if (isinstance(name, str) and name.strip()) or (field == 'field25' and record.get('field25')):
            name = name if isinstance(name, str) else ''
            result.append({'email': email, 'company_name': name.strip(), 'explicit': True,
                # Eightからのコピーか不明なlookupも、独立と推定せず同じ系統に束ねる。
                'origin': 'zoho-eight', 'source': field, 'source_id': 'zoho:Contacts:' + str(record['id']),
                'observed_at': record.get('Modified_Time'), 'source_date_kind': 'record_modified',
                'check_only': True,
                **({'lookup_id': str((record.get('field25') or {}).get('id') or ''),
                    'company_id': lookup_company_id} if field == 'field25' else {})})
    return result
