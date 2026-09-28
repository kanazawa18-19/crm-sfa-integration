"""外部登録時の専用項目と、確定済み関連IDだけを対応付ける。"""
from src.hub_creation.domain import CreationHeld

# API必須項目の実測に基づく。関連名から同名の外部レコードを推測しない。
ZOHO_RELATIONS = {
    'contact': {'field25': ('取引先マスター', 'client_master')},
    'project': {'Account_Name': ('取引先マスター', 'client_master'), 'field10': ('連絡先', 'contact')},
    'product': {'field7': ('先方担当者', 'contact'), 'field12': ('案件管理', 'project')},
    'action': {'field6': ('👨‍👩‍👧‍👦 取引先マスター', 'client_master')},
}


def validate_lead_source_metadata(payload, fields):
    """表示名と内部値の実設定が変わっていたら、既知の対応で作成しない。"""
    from src.sync_engine.webhook_handlers.zoho_field_transforms import LEAD_SOURCE_API_LABELS
    for code in payload.get('field65', []):
        if code not in LEAD_SOURCE_API_LABELS:
            continue
        metadata = [field for field in fields if field.get('api_name') == 'field65']
        options = metadata[0].get('pick_list_values', []) if len(metadata) == 1 else []
        matches = [item for item in options if item.get('actual_value') == code]
        if (len(matches) != 1 or matches[0].get('display_value') != LEAD_SOURCE_API_LABELS[code]
                or matches[0].get('type') == 'unused'):
            raise CreationHeld('リードソースの表示名と内部値の設定が変わっています。対応を確認してください')


def complete_zoho_payload(db_key, properties, payload, *, get_mapping):
    result = dict(payload)
    if db_key == 'client_master':
        result['field4'] = properties.get('取引先名')
    if db_key == 'project':
        result['Closing_Date'] = properties.get('完了予定日')
    if db_key == 'contact':
        # 読み取り専用のFull_Nameは送らず、本人が別欄に入力した姓・名を使う。
        result.pop('Full_Name', None)
        result['Last_Name'] = properties.get('姓')
        result['First_Name'] = properties.get('名')
    for code, (name, target_db) in ZOHO_RELATIONS.get(db_key, {}).items():
        ids = properties.get(name)
        if not ids:
            continue
        if not isinstance(ids, (list, tuple)) or len(ids) != 1 or not isinstance(ids[0], str):
            raise CreationHeld('外部登録の関連先を1件に確定してください: ' + name)
        mapping = get_mapping(ids[0]) if get_mapping else None
        if mapping is None or mapping.db_key != target_db or not mapping.zoho_id:
            raise CreationHeld('関連先のZoho登録を先に完了してください: ' + name)
        result[code] = {'id': str(mapping.zoho_id)}
    return {code: value for code, value in result.items() if value not in (None, '', [], {})}


def kintone_choice_payload(db_key, properties, fields):
    """取得した選択肢を既存の受信変換で逆照合し、一意な戻し先だけを採用する。"""
    from src.sync_engine.webhook_handlers.kintone_field_transforms import KINTONE_FIELD_TRANSFORMS
    result = {}
    for code, (name, transform) in KINTONE_FIELD_TRANSFORMS.get(db_key, {}).items():
        field = fields.get(code, {})
        if field.get('type') not in {'DROP_DOWN', 'RADIO_BUTTON'} or name not in properties:
            continue
        value = properties[name]
        if value in (None, '', [], {}):
            continue
        candidates = []
        for option in field.get('options', {}):
            try:
                if transform(option) == value:
                    candidates.append(option)
            except (ValueError, TypeError):
                continue
        if value in candidates:
            result[code] = value
        elif len(candidates) == 1:
            result[code] = candidates[0]
    return result


def creation_rollup(raw):
    """担当営業・提案サービスの確定済み集計値だけを読む。未完了は補わない。"""
    if not isinstance(raw, dict) or raw.get('type') != 'rollup' or raw.get('has_more'):
        return None
    rollup = raw.get('rollup', {})
    if rollup.get('type') != 'array':
        return None
    result = []
    for entry in rollup.get('array', []):
        kind = entry.get('type')
        if kind == 'people':
            values = [value['id'] for value in entry.get('people', [])]
        elif kind == 'multi_select':
            values = [value['name'] for value in entry.get('multi_select', [])]
        elif kind == 'select':
            values = [entry['select']['name']] if entry.get('select') else []
        else:
            return None
        result.extend(values)
    return list(dict.fromkeys(result))
