"""外部の必須項目と重複を読んでから、予約済みの1回だけ作成する。"""
from __future__ import annotations
import math
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode
from src.db_schema.base import Tool
from src.db_schema.registry import get_schema
from src.sync_engine.clients._http import raise_for_error, ApiError, request_with_retry
from src.sync_engine.outbound_field_mapping import translate_properties, zoho_outbound_field_names, kintone_outbound_field_names
from src.sync_engine.outbound_value_mapping import translate_choice_value
from src.hub_creation.domain import CreationHeld, CreationNotApplicable, identity_hash


def missing_required_reason(required, fields, payload, table, properties):
    """入力で直る不足と、コードの対応付けが必要な不足を分ける。値は表示しない。"""
    reverse = {external: name for name, external in table.items()}
    messages = []
    for code in sorted(required):
        if payload.get(code) not in (None, "", [], {}):
            continue
        field = fields.get(code, {})
        label = str(field.get("field_label") or field.get("label") or code)[:100]
        name = reverse.get(code)
        if name is None:
            kind = "対応未実装"
        elif properties.get(name) in (None, "", [], {}):
            kind = "入力が必要（Notion: " + name + "）"
        else:
            kind = "値の変換が未対応（Notion: " + name + "）"
        messages.append(label + " / " + code + ": " + kind)
    return "必須項目を確認してください: " + "・".join(messages) if messages else None


def normalize_creation_payload(payload, fields, *, kintone=False, lookup_fields=frozenset(), user_fields=frozenset()):
    """取得した型に合うスカラー値だけを予約前に確認する。業務値は推測しない。"""
    text_types = ({"SINGLE_LINE_TEXT", "MULTI_LINE_TEXT", "LINK", "RADIO_BUTTON", "DROP_DOWN", "DATE", "DATETIME", "TIME"}
                  if kintone else {"text", "textarea", "email", "phone", "website", "picklist", "date", "datetime"})
    number_types = {"NUMBER"} if kintone else {"integer", "bigint", "double", "decimal", "currency", "percent"}
    result = {}
    for code, value in payload.items():
        field = fields.get(code, {})
        kind = field.get("type" if kintone else "data_type")
        reason = "項目の型を確認してください: " + code
        if kind in number_types:
            if value is None or value == "":
                result[code] = value
                continue
            if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                raise CreationHeld(reason)
            try:
                number = Decimal(str(value).strip())
            except InvalidOperation:
                raise CreationHeld(reason) from None
            if not number.is_finite():
                raise CreationHeld(reason)
            if kind in {"integer", "bigint"}:
                if number != number.to_integral_value():
                    raise CreationHeld(reason)
                value = int(number)
            else:
                value = float(number)
                if not math.isfinite(value):
                    raise CreationHeld(reason)
        elif kind in text_types:
            if value is not None and not isinstance(value, str):
                raise CreationHeld(reason)
        elif not kintone and kind in {'lookup', 'ownerlookup'} and code in lookup_fields:
            if not isinstance(value, dict) or set(value) != {'id'} or not isinstance(value['id'], str) or not value['id'].isdigit():
                raise CreationHeld(reason)
        elif kintone and kind == 'USER_SELECT' and code in user_fields:
            if not isinstance(value, list) or any(not isinstance(item, dict) or set(item) != {'code'} or not isinstance(item['code'], str) or not item['code'] for item in value):
                raise CreationHeld(reason)
        elif kind in ({'MULTI_SELECT', 'CHECK_BOX'} if kintone else {'multiselectpicklist'}):
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise CreationHeld(reason)
            options = field.get('options', {}) if kintone else {item.get('actual_value') for item in field.get('pick_list_values', [])}
            if not options or any(item not in options for item in value):
                raise CreationHeld(reason)
        elif not kintone and kind == "boolean":
            if not isinstance(value, bool):
                raise CreationHeld(reason)
        else:
            # lookupや複合型はIDや構造の意味を推測して送らない。
            raise CreationHeld(reason + "（未対応の型）")
        result[code] = value
    return result


class ZohoCreationAdapter:
    target = "zoho"

    def __init__(self, client, store=None):
        self.client = client
        self.store = store

    def _get(self, path):
        response = self.client._request("GET", path)
        if response.status_code == 204:
            return {}
        raise_for_error(response, ApiError)
        return response.json()

    def plan(self, db_key, properties):
        module = get_schema(db_key).zoho_api_module
        payload, _ = translate_properties(zoho_outbound_field_names(), db_key, dict(properties), translate_choice_value)
        from src.hub_creation.creation_payload import complete_zoho_payload
        payload = complete_zoho_payload(db_key, properties, payload,
            get_mapping=self.store.get if self.store is not None else None)
        if db_key == 'project':
            from src.sync_engine.decided_choices import CONTROLLERS, choices, merge_choice_memo
            selected = choices(properties.get('サイトコントローラー'), CONTROLLERS)
            if selected:
                try:
                    payload['field70'] = merge_choice_memo(payload.get('field70', ''), 'サイトコントローラー', selected, limit=2000)
                except ValueError as exc:
                    raise CreationHeld(str(exc)) from None
        fields = self._get("/settings/fields?" + urlencode({"module": module})).get("fields")
        layouts = self._get("/settings/layouts?" + urlencode({"module": module})).get("layouts")
        if not fields or not layouts:
            raise CreationHeld("外部の必須項目を確認できません")
        if any(not isinstance(layout.get("sections"), list) for layout in layouts):
            raise CreationHeld("レイアウトの必須項目を確認できません")
        required = {f["api_name"] for f in fields if f.get("system_mandatory")}
        for layout in layouts:
            for section in layout.get("sections", []):
                required.update(f["api_name"] for f in section.get("fields", []) if f.get("required") or f.get("system_mandatory"))
        from src.sync_engine.owner_mapping import owner_payload
        from src.sync_engine.owner_mapping import OWNER_PROPERTIES
        owner_properties = dict(properties)
        if 'Owner' in required and db_key in OWNER_PROPERTIES:
            owner_properties.setdefault(OWNER_PROPERTIES[db_key], [])
        if db_key in OWNER_PROPERTIES and ('Owner' in {field['api_name'] for field in fields} or OWNER_PROPERTIES[db_key] in properties):
            from src.sync_engine.owner_mapping import owner_to_external, UNRESOLVED_OWNER
            resolved_owner = owner_to_external(owner_properties.get(OWNER_PROPERTIES[db_key], []), 'zoho', required='Owner' in required)
            if resolved_owner is UNRESOLVED_OWNER:
                raise CreationHeld('担当者対応の設定または複数担当の対応を確認してください')
        owners, _ = owner_payload(db_key, owner_properties, 'zoho', required='Owner' in required)
        payload.update(owners)
        if 'Owner' not in required and payload.get('Owner') is None:
            payload.pop('Owner', None)
        from src.hub_creation.creation_payload import ZOHO_RELATIONS
        guidance = dict(zoho_outbound_field_names().get(db_key, {}))
        guidance.update({name: code for code, (name, _) in ZOHO_RELATIONS.get(db_key, {}).items()})
        if db_key in OWNER_PROPERTIES:
            guidance[OWNER_PROPERTIES[db_key]] = 'Owner'
        if 'Owner' in required and not payload.get('Owner'):
            raise CreationHeld('確認済みの担当者対応と必須時の代替担当者を設定してください')
        reason = missing_required_reason(required, {f["api_name"]: f for f in fields}, payload,
                                         guidance, properties)
        if reason:
            raise CreationHeld(reason)
        from src.hub_creation.creation_payload import ZOHO_RELATIONS
        payload = normalize_creation_payload(payload, {f["api_name"]: f for f in fields},
                                             lookup_fields=frozenset(ZOHO_RELATIONS.get(db_key, {})) | {"Owner"})
        if db_key == 'contact':
            self._check_contact_duplicates(module, properties)
            return payload
        title_field = {"client_master": "Account_Name", "chain": "Name", "product": "Product_Name", "action": "Name", "project": "Deal_Name"}[db_key]
        title = payload.get(title_field)
        if not isinstance(title, str) or not title.strip():
            raise CreationHeld("名前による重複確認を安全に行えません")
        self._check_list_duplicates(db_key, module, title_field, title)
        return payload

    def _check_contact_duplicates(self, module, properties):
        seen = set()
        surname, given, email = (properties.get('姓') or '').strip(), (properties.get('名') or '').strip(), properties.get('メールアドレス')
        for page in range(1, 11):
            data = self._get(f"/{module}?" + urlencode({'fields': 'id,Last_Name,First_Name,Email', 'per_page': 200, 'page': page}))
            records, info = data.get('data'), data.get('info')
            if not isinstance(records, list) or not isinstance(info, dict) or not isinstance(info.get('more_records'), bool):
                raise CreationHeld('連絡先の重複を最後まで確認できません')
            for record in records:
                identifier = record.get('id')
                if not isinstance(identifier, str) or not identifier or identifier in seen:
                    raise CreationHeld('連絡先の一覧照合結果を確認できません')
                seen.add(identifier)
                if ((str(record.get('Last_Name') or '').strip() == surname and str(record.get('First_Name') or '').strip() == given)
                        or (email and str(record.get('Email') or '').casefold() == email.casefold())):
                    raise CreationHeld('外部に姓名またはメールが一致する連絡先があります。統合候補を確認してください')
            if not info['more_records']:
                return
            if not records:
                break
        raise CreationHeld('連絡先の重複を最後まで確認できません')

    def _check_list_duplicates(self, db_key, module, title_field, title):
        """検索索引の遅延を避け、上限内の通常一覧を最後まで照合する。"""
        expected = identity_hash(db_key, title)
        seen = set()
        for page in range(1, 11):
            try:
                data = self._get(f"/{module}?" + urlencode({"fields": f"id,{title_field}", "per_page": 200, "page": page}))
                records, info = data.get("data"), data.get("info")
                if (not isinstance(records, list) or len(records) > 200
                        or not isinstance(info, dict) or not isinstance(info.get("more_records"), bool)):
                    raise CreationHeld("外部の一覧照合結果を確認できません")
                for record in records:
                    if (not isinstance(record, dict) or not isinstance(record.get("id"), str)
                            or not record["id"] or record["id"] in seen
                            or not isinstance(record.get(title_field), str)):
                        raise CreationHeld("外部の一覧照合結果を確認できません")
                    seen.add(record["id"])
                    if identity_hash(db_key, record[title_field]) == expected:
                        raise CreationHeld("外部に同名の候補があります。重複かどうかの確認が必要です")
                if not info["more_records"]:
                    return
                if not records:
                    raise CreationHeld("外部の一覧照合結果を確認できません")
            except CreationHeld:
                raise
            except Exception:
                raise CreationHeld("外部の一覧を最後まで取得できません。作成を保留します") from None
        raise CreationHeld("外部の一覧が2000件の照合上限を超えるため作成を保留します")

    def create(self, db_key, payload):
        return self.client.insert_record(get_schema(db_key).zoho_api_module, payload)


class KintoneCreationAdapter:
    target = "kintone"

    def __init__(self, targets, store=None):
        self.targets = targets
        self.store = store

    def plan(self, db_key, properties):
        if db_key not in {"client_master", "project", "action"}:
            raise CreationNotApplicable("対応するkintoneアプリがありません")
        target = self.targets.get(db_key)
        if target is None:
            raise CreationHeld("対応するkintoneアプリの接続設定が不足しています")
        client, app = target._client, target._app
        def get(path, params):
            response = request_with_retry("GET", f"https://{client._domain}/k/v1/{path}",
                                          headers=client._headers(has_json_body=False), params=params,
                                          timeout=client._timeout, max_retries=client._max_retries, backoff_base=client._backoff_base)
            raise_for_error(response, ApiError)
            return response.json()
        fields = get("app/form/fields.json", {"app": app}).get("properties")
        if not fields:
            raise CreationHeld("外部の必須項目を確認できません")
        from src.sync_engine.sync_targets.kintone_sync import _choice_value, _memo_choices
        from src.sync_engine.decided_choices import merge_choice_memo
        from src.sync_engine.owner_mapping import owner_to_external, UNRESOLVED_OWNER
        payload, _ = translate_properties(kintone_outbound_field_names(), db_key, dict(properties), _choice_value)
        from src.hub_creation.creation_payload import kintone_choice_payload
        payload.update(kintone_choice_payload(db_key, properties, fields))
        for name, values in _memo_choices(db_key, properties).items():
            payload['文字列__複数行_'] = merge_choice_memo(payload.get('文字列__複数行_', ''), name, values, limit=65535)
        if db_key == 'project':
            payload['日付_0'] = properties.get('作成日')
        if db_key in {'project', 'action'}:
            owner_code = '営業担当者' if db_key == 'project' else 'cnctorMember'
            owner_name = '担当メンバー' if db_key == 'project' else '担当営業'
            if db_key == 'action' and '担当営業' not in properties:
                raise CreationHeld('担当営業の集計が未確定です。Notionの関連先と集計完了を確認してください')
            owner = owner_to_external(properties.get(owner_name, []), 'kintone', required=bool(fields.get(owner_code, {}).get('required')))
            if owner is UNRESOLVED_OWNER:
                raise CreationHeld('担当者対応の設定または複数担当の対応を確認してください')
            payload[owner_code] = owner
            if db_key == 'action':
                services = properties.get('提案サービス')
                if services:
                    payload['service'] = services
                ids = properties.get('👨‍👩‍👧‍👦 取引先マスター')
                if isinstance(ids, (list, tuple)) and len(ids) == 1 and self.store is not None:
                    mapping = self.store.get(ids[0])
                    client_target = self.targets.get('client_master')
                    if mapping is not None and mapping.db_key == 'client_master' and mapping.kintone_id and client_target:
                        record = client_target._client.get_record(client_target._app, mapping.kintone_id)
                        if record and isinstance(record.get('顧客名'), str):
                            payload['client_name'] = record['顧客名']
        payload = {code: value for code, value in payload.items() if value not in (None, '', [], {})}
        if db_key == "client_master" and properties.get("顧客種別"):
            choice = properties["顧客種別"]
            if choice not in fields.get("顧客種別", {}).get("options", {}):
                raise CreationHeld("顧客種別の選択肢がkintoneと一致しません")
            payload["顧客種別"] = choice
        for code, field in fields.items():
            # 実APIの既定値だけを尊重する。選択肢に無い値や他の型は補完しない。
            default = field.get("defaultValue")
            if (code not in payload and field.get("type") == "RADIO_BUTTON"
                    and isinstance(default, str) and default in field.get("options", {})):
                payload[code] = default
        table = dict(kintone_outbound_field_names().get(db_key, {}))
        if db_key == "client_master":
            table["顧客種別"] = "顧客種別"
        reason = missing_required_reason({code for code, field in fields.items() if field.get("required")},
                                         fields, payload, table, properties)
        if reason:
            raise CreationHeld(reason)
        payload = normalize_creation_payload(payload, fields, kintone=True, user_fields={'営業担当者', 'cnctorMember'})
        if db_key != "client_master":
            name_code = '店舗名' if db_key == 'project' else 'client_name'
            name = payload.get(name_code)
            if not isinstance(name, str) or not name:
                raise CreationHeld('重複確認用の名前が未入力です')
            escaped = name.replace('\\', '\\\\').replace('"', '\\"')
            last_id = '0'
            for _ in range(4):
                data = get('records.json', {'app': app, 'query': f'{name_code} = "{escaped}" and $id > {last_id} order by $id asc limit 500'})
                records = data.get('records')
                if not isinstance(records, list):
                    raise CreationHeld('kintoneの重複照合を確認できません')
                for record in records:
                    identifier = record.get('$id', {}).get('value')
                    if not isinstance(identifier, str) or not identifier.isdigit() or int(identifier) <= int(last_id):
                        raise CreationHeld('kintoneの一覧を最後まで確認できません')
                    last_id = identifier
                    comparable = {}
                    for code in payload:
                        native = record.get(code)
                        if not isinstance(native, dict) or 'value' not in native:
                            raise CreationHeld('kintoneの候補の項目を確認できません')
                        value = native['value']
                        if fields.get(code, {}).get('type') == 'USER_SELECT':
                            if not isinstance(value, list) or any(not isinstance(item, dict) or 'code' not in item for item in value):
                                raise CreationHeld('kintoneの候補の担当者を確認できません')
                            value = [{'code': item['code']} for item in value]
                        comparable[code] = value
                    comparable = normalize_creation_payload(comparable, fields, kintone=True, user_fields={'営業担当者','cnctorMember'})
                    same = all((sorted(value, key=str) == sorted(comparable[code], key=str) if isinstance(value, list)
                                else value == comparable[code]) for code, value in payload.items())
                    if db_key == 'project' or same:
                        raise CreationHeld('kintoneに同名案件があります。比較してください' if db_key == 'project' else 'kintoneに登録内容が一致する候補があります。統合候補を確認してください')
                if len(records) < 500:
                    return payload
            raise CreationHeld('kintoneの候補が照合上限を超えるため作成を保留します')
        if not fields.get("顧客名", {}).get("unique"):
            raise CreationHeld("顧客名の重複禁止設定を確認できません")
        name = payload.get("顧客名")
        if not isinstance(name, str) or not name:
            raise CreationHeld("顧客名が未入力です")
        escaped = name.replace("\\", "\\\\").replace('"', '\\"')
        if get("records.json", {"app": app, "query": f'顧客名 = "{escaped}" limit 1', "fields[0]": "$id"}).get("records"):
            raise CreationHeld("kintoneに同名の顧客があります。重複かどうかの確認が必要です")
        return payload

    def create(self, db_key, payload):
        target = self.targets[db_key]
        return target._client.add_record(target._app, payload)
