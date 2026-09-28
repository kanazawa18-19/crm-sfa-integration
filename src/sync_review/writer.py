"""承認台帳に保存された1項目だけを書き換える。"""
from __future__ import annotations

from src.db_schema.base import Tool, PropertyType
from src.sync_review.choice_storage import companion, storage_field, split_block, enrich_record, VIRTUAL_CONTROLLER_FIELD
from src.sync_review.domain import ReviewConflict, is_blank, values_equal


class ApprovedFieldWriter:
    def __init__(self, dispatcher):
        self.dispatcher = dispatcher

    def plan(self, row, current, prop):
        if row['state'] == 'restore_requested' or row['progress'].get('_restore'):
            restore = row['progress'].get('_restore')
            if not restore or 'value' not in restore:
                raise ReviewConflict('復元値を選択してください')
            # 復元は空欄にされた元ツールだけ。他方の値は上書きしない。
            source = row['sourceTool']
            desired = self._restore_value(Tool(source), row['dbKey'], prop.name, restore['value'])
            if source == Tool.ZOHO.value and row['dbKey'] == 'project' and prop.name == 'メモ':
                from src.sync_engine.decided_choices import replace_memo_body
                desired = replace_memo_body(current[source]['value'], desired)
            return {source: desired}
        result = {}
        for tool, item in current.items():
            if not item['supported']:
                raise ReviewConflict('対応する項目がありません: ' + tool)
            value = item['value']
            if is_blank(value):
                result[tool] = value
            elif Tool(tool) is Tool.KINTONE:
                if row['dbKey'] == 'project' and prop.name == '担当メンバー':
                    from src.sync_engine.owner_mapping import owner_to_external, UNRESOLVED_OWNER
                    fallback = owner_to_external([], 'kintone', required=True)
                    if fallback is UNRESOLVED_OWNER:
                        raise ReviewConflict('必須担当者の代替対応が未確認です')
                    result[tool] = fallback
                else:
                    result[tool] = [] if isinstance(value, list) else ''
            elif Tool(tool) is Tool.SPREADSHEET:
                result[tool] = ''
            elif Tool(tool) is Tool.NOTION:
                result[tool] = ([] if prop.property_type in {PropertyType.RELATION, PropertyType.USER, PropertyType.MULTI_SELECT}
                                else '' if prop.property_type in {PropertyType.TEXT, PropertyType.TITLE, PropertyType.JSON_TEXT} else None)
            else:
                if row['dbKey'] == 'project' and prop.name == 'メモ':
                    from src.sync_engine.decided_choices import replace_memo_body
                    result[tool] = replace_memo_body(value, '')
                else:
                    result[tool] = [] if isinstance(value, list) else None
        return result

    def companion_plan(self, row, current, prop):
        result = {tool: item.get('companion') for tool, item in current.items()}
        restore = row['progress'].get('_restore')
        for tool, item in current.items():
            if item.get('companion') is None:
                continue
            if restore:
                if tool != row['sourceTool']:
                    continue
                from src.sync_engine.decided_choices import merge_choice_memo
                value = restore['value']
                if not isinstance(value, list):
                    raise ReviewConflict('全選択の復元値を確認できません')
                result[tool] = merge_choice_memo('', prop.name, value, limit=65535)
            else:
                result[tool] = ''
        return result

    def _restore_value(self, tool, db_key, name, value):
        if tool in {Tool.NOTION, Tool.SPREADSHEET}:
            return value
        if tool is Tool.KINTONE and db_key == 'project' and name == 'サイトコントローラー':
            return value
        target = self.dispatcher._targets[tool]
        child = target._resolve(db_key) if hasattr(target, '_resolve') else target
        payload = (child._to_zoho_payload({name: value}, db_key) if tool is Tool.ZOHO
                   else child._to_kintone_payload({name: value}, db_key))
        if not payload or len(payload) != 1:
            raise ReviewConflict('復元値の外部対応を確認できません')
        return next(iter(payload.values()))

    def write(self, mapping, prop, tool, expected, desired, *, desired_companion=None):
        target = self.dispatcher._targets[tool]
        if tool is Tool.SPREADSHEET:
            # 既存の同期キー検査と同時更新ロックを共用し、新規行は作らない。
            current = target.get_record_by_sync_key(mapping.notion_key, db_key=mapping.db_key)
            if current is None or not values_equal(current.get(expected['field']), expected['value']):
                raise ReviewConflict('シートの対象値が書込み直前に変わりました')
            ok, _ = self.dispatcher._write_spreadsheet_value(target, mapping, {prop.name: desired})
            if not ok:
                raise ReviewConflict('シートへ書き込めません')
            return
        child = target._resolve(mapping.db_key) if hasattr(target, '_resolve') else target
        if tool is Tool.NOTION and hasattr(target, '_fallback_client'):
            from src.sync_engine.webhook_handlers.notion_webhook import parse_notion_property_value
            raw = target._fallback_client.get_raw_page_with_relations(expected['id'],
                {prop.name} if prop.property_type is PropertyType.RELATION else set())
            current = {prop.name: parse_notion_property_value(raw['properties'][prop.name])}
        else:
            current = target.get_record(expected['id'], db_key=mapping.db_key)
        if current is not None:
            current = enrich_record(tool, mapping.db_key, prop.name, current)
        if current is None or not values_equal(current.get(expected['field']), expected['value']):
            raise ReviewConflict('書込み直前に値が変わりました')
        if tool is Tool.NOTION:
            target.upsert_record(expected['id'], {prop.name: desired}, db_key=mapping.db_key)
        elif tool is Tool.ZOHO:
            if not child._enabled or not current.get('Modified_Time'):
                raise ReviewConflict('Zohoの有効状態または版を確認できません')
            payload = self._payload_with_companion(tool, mapping, prop, current, expected, desired, desired_companion)
            child._client.update_record(child._module, expected['id'], payload,
                                        expected_version=current['Modified_Time'])
        elif tool is Tool.KINTONE:
            if current.get('$revision') is None:
                raise ReviewConflict('kintoneの版を確認できません')
            payload = self._payload_with_companion(tool, mapping, prop, current, expected, desired, desired_companion)
            child._client.update_record(child._app, expected['id'], payload,
                                        expected_version=str(current['$revision']))


    @staticmethod
    def _payload_with_companion(tool, mapping, prop, current, expected, desired, desired_companion):
        payload = {} if expected['field'] == VIRTUAL_CONTROLLER_FIELD else {expected['field']: desired}
        field = storage_field(tool, mapping.db_key, prop.name)
        if field:
            if companion(tool, mapping.db_key, prop.name, current) != expected.get('companion'):
                raise ReviewConflict('書込み直前に全選択保存枠が変わりました')
            body, _ = split_block(current.get(field), prop.name)
            payload[field] = body + ('\n\n' if body and desired_companion else '') + (desired_companion or '')
        if tool is Tool.ZOHO and isinstance(payload.get('field70'), str) and len(payload['field70']) > 2000:
            raise ReviewConflict('メモと全選択保存枠の合計が2000文字を超えます')
        return payload
