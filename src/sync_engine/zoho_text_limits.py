"""Zoho自由記述の文字上限。原文・識別子は変更せず、送信する本文だけを短縮する。"""
from contextlib import contextmanager
from contextvars import ContextVar

from src.sync_engine.decided_choices import split_controller_memo
from src.sync_engine.outbound_field_mapping import zoho_outbound_field_names
from src.sync_engine.sync_notes import TEXT_LIMITS, UNAVAILABLE

TEXT_PROPERTIES = {
    'client_master': frozenset({'備考'}),
    'chain': frozenset({'メモ'}),
    'project': frozenset({'メモ'}),
    'action': frozenset({'履歴メモ'}),
}
_notes = ContextVar('zoho_text_limit_notes', default=None)
_metadata = ContextVar('zoho_text_limit_metadata', default=None)


@contextmanager
def collect_text_limit_notes():
    token = _notes.set({})
    metadata_token = _metadata.set({})
    try:
        yield
    finally:
        _notes.reset(token)
        _metadata.reset(metadata_token)


def cached_metadata(client, module, reader):
    cache = _metadata.get()
    if cache is None:
        return reader(module)
    key = (id(client), module)
    if key not in cache:
        cache[key] = reader(module)
    return cache[key]


def ambiguous_shortening(db_key, name, incoming, original):
    return (name in TEXT_PROPERTIES.get(db_key, ()) and isinstance(incoming, str)
            and isinstance(original, str) and bool(incoming)
            and len(incoming) < len(original) and original.startswith(incoming))


def ambiguity_key(name):
    return f'{UNAVAILABLE}|zoho:短縮確認（{name}）'


def ambiguity_note(name):
    return {ambiguity_key(name): f'[zoho:短縮確認（{name}）] 以前の文字数上限による省略か本文の削除編集か判断できないため、逆同期を保留しました。原文は保持しています。意図した短縮はNotionで編集してください。'}


def collected_notes():
    return dict(_notes.get() or {})


def record_notes(notes):
    current = _notes.get()
    if current is not None:
        current.update(notes)


class TextLimitedPayload(dict):
    """作成予約前に確定した送信値と、成功時に残す省略記録。"""
    def __init__(self, payload, notes, planned_notes=None):
        super().__init__(payload)
        self.text_limit_notes = notes
        self.planned_text_limit_notes = planned_notes or {}


def text_limit_key(name):
    return f'{TEXT_LIMITS}|zoho:文字数制限（{name}）'


def text_limit_note(name, limit, original_length, sent_length, *, state='sent'):
    status = {'planned': '省略して送信する予定です', 'sent': '省略して送信しました'}[state]
    return (f'[zoho:文字数制限（{name}）] Zohoの上限{limit}文字に合わせ、'
            f'{original_length}文字から{sent_length}文字に{status}。Notionの原文は保持しています。')


def creation_notes_pending(attempt):
    import json
    if not attempt or attempt.get('state') != 'created':
        return False
    try:
        saved = json.loads(attempt.get('reason') or '{}')
        return isinstance(saved, dict) and 'text_limit_notes' in saved and not saved.get('text_limit_notes_recorded')
    except (ValueError, TypeError):
        return False


def recovered_creation_notes(attempt, db_key, properties):
    """作成確定と同時に保存した省略記録を再利用する。欠けた記録は推測しない。"""
    import json
    try:
        saved = json.loads(attempt.get('reason') or '{}')
        if saved.get('text_limit_notes_recorded'):
            return {}
        notes = saved.get('text_limit_notes')
        allowed = {text_limit_key(name) for name in TEXT_PROPERTIES.get(db_key, ())}
        if isinstance(notes, dict) and all(key in allowed and isinstance(value, str) for key, value in notes.items()):
            return notes
    except (ValueError, AttributeError, TypeError):
        pass
    return {text_limit_key(name): f'[zoho:文字数制限（{name}）] 登録済み。省略結果の記録は未確認です。Notionの原文は保持しています。'
            for name in TEXT_PROPERTIES.get(db_key, ()) if properties.get(name)}


def text_fields(db_key, payload):
    table = zoho_outbound_field_names().get(db_key, {})
    return {name: table[name] for name in TEXT_PROPERTIES.get(db_key, ())
            if name in table and table[name] in payload}


def normalized_text_record(db_key, record):
    """比較に必要な4項目だけ日本語キーを補う。APIの生値・版は残す。"""
    result = dict(record)
    for name, code in text_fields(db_key, record).items():
        if name in result:
            continue
        value = record[code]
        if db_key == 'project' and name == 'メモ':
            value = split_controller_memo(value)[0]
        result[name] = value
    return result


def shorten(value, limit, *, managed=False):
    if len(value) <= limit:
        return value
    body, block = split_controller_memo(value) if managed else (value, '')
    room = limit - len(block)
    if room < 0:
        raise ValueError('管理用メモだけで文字数上限を超えるため保持します')
    body = body[:max(0, room - 2)] if block else body[:room]
    return body + ('\n\n' if block and body else '') + block


def limit_payload(db_key, payload, fields):
    """自由記述と実metadataの型・上限が一致したものだけを処理する。"""
    result, notes, planned = dict(payload), {}, {}
    metadata = {field.get('api_name'): field for field in fields}
    for name, code in text_fields(db_key, payload).items():
        value, field = payload[code], metadata.get(code, {})
        limit = field.get('length')
        if (not isinstance(value, str) or field.get('data_type') not in {'text', 'textarea'}
                or type(limit) is not int or limit <= 0):
            continue
        result[code] = shorten(value, limit, managed=db_key == 'project' and name == 'メモ')
        key = text_limit_key(name)
        notes[key] = text_limit_note(name, limit, len(value), len(result[code])) if result[code] != value else ''
        planned[key] = text_limit_note(name, limit, len(value), len(result[code]), state='planned') if result[code] != value else ''
    return TextLimitedPayload(result, notes, planned)
