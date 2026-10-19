"""外部接続なしで添付の識別・容量・宛先を判定する。"""
from dataclasses import dataclass
import hashlib
import json
import re
from urllib.parse import urlsplit

MAX_BYTES = 10 * 1024 * 1024
CATEGORIES = {
    'project': ('見積書', '申込書・契約書', '提案資料', '添付ファイル'),
    'action': ('手当情報アップロード', '添付ファイル'),
}


class Held(ValueError):
    """安全に確定できないため、内容を変えず保留する。"""


@dataclass(frozen=True)
class Source:
    tool: str
    db: str
    record: str
    category: str
    external_record: str
    file_id: str
    name: str
    size: int
    owner: str = ''
    field: str = ''

    def __post_init__(self):
        if self.tool not in ('notion', 'zoho', 'kintone', 'gmail') or self.category not in CATEGORIES.get(self.db, ()):
            raise Held('添付の種類が未対応です')
        if not all(isinstance(v, str) and v and len(v) <= 2048 for v in (self.record, self.external_record, self.file_id, self.name)):
            raise Held('添付の識別情報が不足しています')
        if not isinstance(self.size, int) or self.size < 0:
            raise Held('添付容量の形式が不正です')

    @property
    def key(self):
        # 表示名ではなく原本ID。署名付きURLを識別子・DBへ保存しない。
        values = (self.tool, self.db, self.record, self.category, self.external_record, self.file_id, self.owner, self.field)
        return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()


def content_key(source, data):
    if not data or len(data) > MAX_BYTES:
        raise Held('空または10MB超の添付です')
    digest = hashlib.sha256(data).hexdigest()
    identity = '\0'.join((source.db, source.record, source.category, digest))
    return hashlib.sha256(identity.encode()).hexdigest(), digest


def filename(value):
    value = re.sub(r'[\x00-\x1f\x7f/\\]', '_', value).strip()[:180]
    return value or '添付ファイル'


def drive_id(url):
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or parsed.hostname != 'drive.google.com' or parsed.username or parsed.port:
        return None
    match = re.fullmatch(r'/file/d/([\w-]+)/view', parsed.path)
    return match.group(1) if match else None


def notion_download_url(url):
    parsed = urlsplit(url)
    # 外部リンク一般にはアクセスしない。Notionから直前に取得した署名付き実体だけ。
    hosts = {'prod-files-secure.s3.us-west-2.amazonaws.com', 's3.us-west-2.amazonaws.com'}
    if parsed.scheme != 'https' or parsed.hostname not in hosts or parsed.username or parsed.port:
        raise Held('この外部添付URLは自動取得対象外です')
    if parsed.hostname == 's3.us-west-2.amazonaws.com' and not parsed.path.startswith('/secure.notion-static.com/'):
        raise Held('Notion以外の配信先です')
    return url
