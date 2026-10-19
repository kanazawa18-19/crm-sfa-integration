"""作成時に反映済みの親関連だけを実物で確認し、配送未完了を解消する。"""
from contextlib import ExitStack

from src.db_schema.base import PropertyType, Tool
from src.db_schema.registry import get_schema
from src.hub_creation.creation_payload import ZOHO_RELATIONS
from src.record_merge.aliases import require_record_available, resolve_alias
from src.sync_engine.record_sync_lock import acquire_record_sync_lock


def _mapping(store, key, identifier):
    if resolve_alias(key, Tool.NOTION, identifier):
        return None
    require_record_available(key, identifier)
    value = store.get(identifier)
    return value if value and (value.db_key, value.notion_key) == (key, identifier) else None


def _external_matches(tool, target, key, mapping, parent):
    if tool is Tool.ZOHO:
        if not mapping.zoho_id or not parent.zoho_id:
            return False
        record = target.get_record(str(mapping.zoho_id), db_key=key)
        company = target.get_record(str(parent.zoho_id), db_key='client_master')
        codes = list(ZOHO_RELATIONS[key])
        if len(codes) != 1:
            return False
        value = (record or {}).get(codes[0])
        return (str((record or {}).get('id')) == str(mapping.zoho_id)
                and str((company or {}).get('id')) == str(parent.zoho_id)
                and isinstance(value, dict) and str(value.get('id')) == str(parent.zoho_id))
    if tool is Tool.KINTONE and key == 'action':
        if not mapping.kintone_id or not parent.kintone_id:
            return False
        record = target.get_record(str(mapping.kintone_id), db_key=key)
        company = target.get_record(str(parent.kintone_id), db_key='client_master')
        name = (company or {}).get('顧客名')
        return (str((record or {}).get('$id')) == str(mapping.kintone_id)
                and str((company or {}).get('$id')) == str(parent.kintone_id)
                and isinstance(name, str) and bool(name.strip())
                and (record or {}).get('client_name') == name)
    return False


def _completed_at_same_time(guard, name, occurred):
    if guard.conn is None:
        row = guard.state.get('field:' + name, {})
    else:
        with guard.conn.cursor() as cursor:
            cursor.execute('SELECT "acceptedAt", "completedAt" FROM "RecordSyncFieldWatermark" '
                           'WHERE "dbKey"=%s AND "notionKey"=%s AND "propertyName"=%s',
                           (guard.db_key, guard.notion_key, name))
            row = cursor.fetchone() or {}
    return row.get('acceptedAt') == occurred and row.get('completedAt') == occurred


def confirm_parent_relation(gateway, wiring, event, result):
    """証拠が揃わない場合は未完了のまま。外部への書込や候補推測は行わない。"""
    if event.db_key not in {'contact', 'action'}:
        return False
    skipped = [item for item in result.properties if item.skipped_tools]
    relations = ZOHO_RELATIONS[event.db_key]
    if len(relations) != 1:
        return False
    name, parent_key = next(iter(relations.values()))
    prop = next((p for p in get_schema(event.db_key).properties if p.name == name), None)
    if not prop or prop.property_type is not PropertyType.RELATION or prop.relation_target != parent_key:
        return False
    allowed = {Tool.ZOHO} | ({Tool.KINTONE} if event.db_key == 'action' else set())
    # 所属だけの再配送は、既に同時刻で完了していると通常同期が省略する。
    # 他の項目を含む省略は成功扱いせず、この関連を再び実物照合する。
    already_completed = (result.skipped and result.reason == 'stale_event'
                         and set(event.properties) == {name}
                         and event.expected_source_properties == event.properties)
    if result.skipped and not already_completed:
        return False
    if not skipped and not already_completed:
        return False
    if any(item.property_name != name or item.related_db_key or item.related_notion_key
           or item.review_pending or item.registration_pending_tools
           or not item.skipped_tools <= allowed for item in skipped):
        return False
    ids = event.properties.get(name)
    if not isinstance(ids, (list, tuple)) or len(ids) != 1 or not isinstance(ids[0], str):
        return False
    store = wiring.id_mapping_store
    dispatcher = getattr(wiring.dispatcher, '_dispatcher', wiring.dispatcher)
    targets = dispatcher._targets
    with ExitStack() as stack:
        guards = {key: stack.enter_context(acquire_record_sync_lock(store, *key))
                  for key in sorted({(event.db_key, event.external_id), (parent_key, ids[0])})}
        guard = guards[event.db_key, event.external_id]
        if guard.rejects(event.occurred_at):
            return False
        if already_completed and not _completed_at_same_time(guard, name, event.occurred_at):
            return False
        mapping = _mapping(store, event.db_key, event.external_id)
        parent = _mapping(store, parent_key, ids[0])
        if not mapping or not parent:
            return False
        # dispatcherの鍵は解放済みなので、原本一致を再確認する。
        def source_matches():
            current = gateway.live(event.db_key, event.external_id)
            from datetime import datetime
            edited = datetime.fromisoformat(current['_last_edited_time'].replace('Z', '+00:00'))
            return edited == event.occurred_at and all(
                current.get(k) == v for k, v in event.expected_source_properties.items())
        if not source_matches():
            return False
        tools = allowed if already_completed else set().union(*(item.skipped_tools for item in skipped))
        if not all(tool in targets and _external_matches(tool, targets[tool], event.db_key, mapping, parent)
                   for tool in tools):
            return False
        if not source_matches():
            return False
        guard.save_fields([name], event.occurred_at, completed=True)
        return True
