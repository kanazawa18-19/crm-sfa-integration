"""同名連絡先の別管理に必要な根拠。人物の同一性を推測しない。"""


def distinct_contacts(left, right):
    emails = [v.get('メールアドレス') for v in (left, right)]
    if any(not isinstance(v, str) or not v.strip() for v in emails):
        return False
    if emails[0].strip().casefold() == emails[1].strip().casefold():
        return False
    affiliations = [v.get('取引先マスター') for v in (left, right)]
    if any(not isinstance(v, list) or not v or any(not isinstance(x, str) or not x for x in v)
           for v in affiliations):
        return False
    return set(x.replace('-', '') for x in affiliations[0]).isdisjoint(
        x.replace('-', '') for x in affiliations[1])
