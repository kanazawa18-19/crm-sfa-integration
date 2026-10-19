"""Gmail実物のラベルで下書きと送信済みを区別する純粋な判断。"""
from email.utils import getaddresses


class MailStateUnknown(ValueError):
    pass


def labels(value):
    if not isinstance(value, list) or any(not isinstance(x, str) or not x or len(x) > 200 for x in value) or len(value) > 200:
        return None
    return frozenset(value)


def sent(message):
    values = labels(message.get('label_ids'))
    return values is not None and 'SENT' in values and 'DRAFT' not in values


def eligible(message, owner):
    """下書きは正常な対象外。不明は再取得し、受信の照合用途は維持する。"""
    values = labels(message.label_ids)
    if values is None:
        raise MailStateUnknown('Gmailの送受信状態を確認できません')
    if 'DRAFT' in values:
        return False
    outbound = any(address.casefold() == owner.casefold()
                   for _, address in getaddresses([message.from_header]))
    return not outbound or 'SENT' in values
