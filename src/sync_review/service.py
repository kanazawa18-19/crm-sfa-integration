"""通常同期と同じレコードロック内で削除を保留する。"""
from __future__ import annotations

from src.db_schema.base import Tool, PropertyType
from src.sync_engine.outbound_field_mapping import zoho_outbound_field_names, kintone_outbound_field_names
from src.sync_review.choice_storage import companion, enrich_record, VIRTUAL_CONTROLLER_FIELD
from src.sync_review.domain import is_blank, snapshot_hash, ReviewConflict, values_equal


def external_field(tool, db_key, property_name):
    if tool is Tool.ZOHO:
        return zoho_outbound_field_names().get(db_key, {}).get(property_name)
    if tool is Tool.KINTONE:
        if db_key == 'project' and property_name == 'サイトコントローラー':
            return VIRTUAL_CONTROLLER_FIELD
        return kintone_outbound_field_names().get(db_key, {}).get(property_name)
    return property_name


def external_id(tool, mapping):
    return {Tool.NOTION: mapping.notion_key, Tool.ZOHO: mapping.zoho_id,
            Tool.KINTONE: mapping.kintone_id, Tool.SPREADSHEET: mapping.spreadsheet_row}.get(tool)


UNAVAILABLE = object()


def canonical_value(tool, db_key, prop, record, field):
    """復元可能な元値だけを表示する。関連IDや姓名を推測しない。"""
    if tool is Tool.ZOHO and db_key == 'project' and prop.name == 'サイトコントローラー':
        from src.sync_engine.decided_choices import controller_from_external
        value = controller_from_external(record[field], record.get('field70'))
        return UNAVAILABLE if value is None else value
    if tool is Tool.ZOHO and db_key == 'project' and prop.name == 'メモ':
        from src.sync_engine.decided_choices import split_controller_memo
        return split_controller_memo(record[field])[0]
    if tool is Tool.NOTION or field == VIRTUAL_CONTROLLER_FIELD:
        return record[field]
    if tool is Tool.SPREADSHEET:
        from src.db_schema.base import PropertyType
        if prop.property_type in {PropertyType.TEXT, PropertyType.TITLE, PropertyType.EMAIL,
                                  PropertyType.PHONE, PropertyType.URL}:
            return record[field]
        return UNAVAILABLE
    if tool is Tool.KINTONE:
        from src.sync_engine.webhook_handlers.kintone_field_transforms import KINTONE_FIELD_TRANSFORMS, SKIP_FIELD
        pair = KINTONE_FIELD_TRANSFORMS.get(db_key, {}).get(field)
    else:
        from src.sync_engine.webhook_handlers.zoho_field_transforms import ZOHO_LABEL_FIELD_MAPPINGS, SKIP_FIELD
        from src.sync_engine.zoho_field_mapping import resolve_zoho_field_label
        from src.db_schema.registry import get_schema
        label = resolve_zoho_field_label(get_schema(db_key).zoho_api_module, field)
        pair = ZOHO_LABEL_FIELD_MAPPINGS.get(db_key, {}).get(label)
    if pair is None or pair[0] != prop.name:
        return UNAVAILABLE
    from src.db_schema.base import PropertyType
    if prop.property_type in {PropertyType.RELATION, PropertyType.USER}:
        return UNAVAILABLE
    try:
        value = pair[1](record[field])
    except (ValueError, TypeError):
        return UNAVAILABLE
    return UNAVAILABLE if value is SKIP_FIELD else value


class FieldReviewService:
    def __init__(self, journal, targets):
        self.journal = journal
        self.targets = targets

    def snapshot(self, mapping, prop, records=None):
        records = {} if records is None else records
        result = {}
        for tool, target in self.targets.items():
            if tool is not Tool.NOTION and not prop.should_sync_to(tool):
                continue
            identifier = external_id(tool, mapping)
            field = external_field(tool, mapping.db_key, prop.name)
            if identifier is None and tool is not Tool.SPREADSHEET:
                continue
            if field is None:
                result[tool.value] = {"supported": False, "id": identifier}
                continue
            try:
                record = self._read_record(tool, target, mapping, prop, records)
            except Exception as exc:
                result[tool.value] = {"supported": False, "id": identifier, "error": type(exc).__name__}
                continue
            if record is None:
                if tool is Tool.SPREADSHEET and identifier is None:
                    continue
                result[tool.value] = {"supported": False, "id": identifier, "error": "record_unavailable"}
                continue
            try:
                record = enrich_record(tool, mapping.db_key, prop.name, record)
                if field is None or field not in record:
                    result[tool.value] = {"supported": False, "id": identifier}
                    continue
                result[tool.value] = {"supported": True, "id": identifier,
                                      "field": field, "value": record[field],
                                      "companion": companion(tool, mapping.db_key, prop.name, record)}
                canonical = canonical_value(tool, mapping.db_key, prop, record, field)
                if canonical is not UNAVAILABLE:
                    result[tool.value]['canonical'] = canonical
            except Exception as exc:
                result[tool.value] = {"supported": False, "id": identifier, "error": type(exc).__name__}
        return result

    @staticmethod
    def _read_record(tool, target, mapping, prop, records):
        # 関連の全件取得は対象の関連だけ。空欄項目ごとに全関連を走査しない。
        cache_key = (tool, prop.name) if tool is Tool.NOTION and prop.property_type is PropertyType.RELATION else tool
        if cache_key in records:
            return records[cache_key]
        identifier = external_id(tool, mapping)
        if tool is Tool.NOTION and hasattr(target, '_fallback_client'):
            from src.sync_engine.webhook_handlers.notion_webhook import parse_notion_property_value
            from src.sync_engine.clients.notion_client import PARSEABLE_NOTION_PROPERTY_TYPES
            names = {prop.name} if prop.property_type is PropertyType.RELATION else set()
            raw = target._fallback_client.get_raw_page_with_relations(identifier, names)
            record = ({name: parse_notion_property_value(value) for name, value in raw['properties'].items()
                       if value.get('type') in PARSEABLE_NOTION_PROPERTY_TYPES} if raw else None)
        elif tool is Tool.SPREADSHEET:
            record = target.get_record_by_sync_key(mapping.notion_key, db_key=mapping.db_key)
        else:
            record = target.get_record(identifier, db_key=mapping.db_key)
        records[cache_key] = record
        return record

    def filter_properties(self, event, mapping, prepared, *, observe_source=True):
        """空欄・承認待ち・空欄維持の項目を、他の通常同期から分離する。"""
        allowed, held = [], []
        records = {}
        active_by_name = self.journal.active_for_record(mapping.db_key, mapping.notion_key)
        previous = {}
        if event.source_tool is Tool.KINTONE:
            previous = self.journal.source_values(mapping.db_key, mapping.notion_key, event.source_tool.value)
        for name, prop, value in prepared:
            active = active_by_name.get(name)
            if active is not None and active['state'] in {'pending', 'confirmed', 'kept_blank'} and (
                active['sourceTool'] == event.source_tool.value and not is_blank(value)
            ):
                current = self.snapshot(mapping, prop, records).get(event.source_tool.value)
                if current and current['supported'] and values_equal(current.get('canonical', current['value']), value):
                    self.journal.finish(active['id'], 'superseded')
                    active = None
            if active is not None:
                # 復旧・消去中の通常Webhookも、途中値を他ツールへ戻さない。
                held.append(name)
                continue
            if not is_blank(value):
                allowed.append((name, prop, value))
                continue
            if not observe_source:
                # 他ツールのページ更新時刻だけでは、この項目の削除意図を確認できない。
                continue
            if event.source_tool is Tool.KINTONE and is_blank(previous.get(name)):
                # 全項目通知の初回空欄は、今回の削除と区別できない。
                continue
            snapshot = self.snapshot(mapping, prop, records)
            source = snapshot.get(event.source_tool.value)
            if source is not None and source['supported'] and not is_blank(
                source.get('canonical') if prop.name == 'メモ' and 'canonical' in source else source.get('value')
            ):
                # 実物に値が戻っている古い空欄イベントは、削除依頼にしない。
                continue
            if source is not None and source['supported'] and not any(
                item['supported'] and not is_blank(item.get('canonical', item.get('value')) if prop.name == 'メモ' else item.get('value')) for item in snapshot.values()
            ):
                # 既に全て空欄なら配送する値が無い。
                continue
            if source is None:
                snapshot[event.source_tool.value] = {'supported': False, 'id': event.external_id,
                                                     'error': 'source_unavailable'}
            self.journal.enqueue(db_key=mapping.db_key, notion_key=mapping.notion_key,
                                 property_name=name, source_tool=event.source_tool.value,
                                 event_at=event.occurred_at, snapshot=snapshot)
            held.append(name)
        if event.source_tool is Tool.KINTONE and observe_source:
            self.journal.observe_source(mapping.db_key, mapping.notion_key, event.source_tool.value,
                {name: value for name, _, value in prepared}, event.occurred_at)
        return allowed, held

    def execute(self, review_id, *, store, writer):
        """確認済みの変更を1送り先ずつ配送し、中断後は実値で続行する。"""
        from src.db_schema.registry import get_schema
        from src.sync_engine.record_sync_lock import acquire_record_sync_lock
        row = self.journal.get(review_id)
        if row is None:
            raise ReviewConflict("対象がありません")
        with acquire_record_sync_lock(store, row['dbKey'], row['notionKey']):
            row = self.journal.get(review_id)
            if row['state'] == 'done':
                return row
            if row['state'] not in {'approved', 'restore_requested'}:
                raise ReviewConflict("承認または復元の選択が必要です")
            mapping = store.get(row['notionKey'])
            if mapping is None or mapping.db_key != row['dbKey']:
                raise ReviewConflict("同期先の対応が変わっています")
            prop = get_schema(row['dbKey']).get_property(row['propertyName'])
            try:
                current = self.snapshot(mapping, prop)
                expected = row['snapshot']
                if set(current) != set(expected):
                    raise ReviewConflict("同期先が変わりました。再確認が必要です")
                plan = writer.plan(row, current, prop)
                # 1件目を書く前に、全配送先の変更と再開済み状態を確認する。
                companion_plan = writer.companion_plan(row, current, prop)
                for tool, old in expected.items():
                    now = current[tool]
                    progress = row['progress'].get(tool)
                    if not old['supported'] or not now['supported']:
                        raise ReviewConflict("項目の対応が未確認です: " + tool)
                    if old['id'] != now['id'] or old['field'] != now['field']:
                        raise ReviewConflict("対象IDまたは項目が変わりました")
                    if old.get('companion') != now.get('companion'):
                        if not progress or now.get('companion') != progress.get('desired_companion'):
                            raise ReviewConflict('確認後に全選択保存枠が変更されました: ' + tool)
                    if not values_equal(old['value'], now['value']):
                        if not progress or not values_equal(now['value'], progress.get('desired')):
                            raise ReviewConflict("確認後に値が変更されました: " + tool)
                for tool, desired in plan.items():
                    # 書込前の記録がある場合だけ、既に目的値の実物を成功として扱う。
                    desired_companion = companion_plan.get(tool)
                    if not values_equal(current[tool]['value'], desired) or current[tool].get('companion') != desired_companion:
                        self.journal.record_progress(review_id, tool, {'desired': desired, 'desired_companion': desired_companion, 'state': 'writing'})
                        writer.write(mapping, prop, Tool(tool), current[tool], desired, desired_companion=desired_companion)
                    actual = self.snapshot(mapping, prop).get(tool)
                    if actual is None or not values_equal(actual.get('value'), desired) or actual.get('companion') != desired_companion:
                        raise ReviewConflict("書込み後の値を確認できません: " + tool)
                    self.journal.record_progress(review_id, tool, {'desired': desired, 'desired_companion': desired_companion, 'state': 'done'})
                self.journal.finish(review_id, 'done')
            except Exception as exc:
                # HTTP本文に個人情報や認証情報が含まれる可能性があるため型だけ保存する。
                self.journal.finish(review_id, 'failed', error=str(exc) if isinstance(exc, ReviewConflict) else type(exc).__name__)
                raise
            return self.journal.get(review_id)
