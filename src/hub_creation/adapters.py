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


def normalize_creation_payload(payload, fields, *, kintone=False):
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

    def __init__(self, client):
        self.client = client

    def _get(self, path):
        response = self.client._request("GET", path)
        if response.status_code == 204:
            return {}
        raise_for_error(response, ApiError)
        return response.json()

    def plan(self, db_key, properties):
        module = get_schema(db_key).zoho_api_module
        payload, _ = translate_properties(zoho_outbound_field_names(), db_key, dict(properties), translate_choice_value)
        # フルネームを姓と決め付けない。対応付け未確定のDBは保留する。
        if db_key == "contact":
            raise CreationHeld("連絡先の姓（Last_Name）の対応が未確定です")
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
        reason = missing_required_reason(required, {f["api_name"]: f for f in fields}, payload,
                                         zoho_outbound_field_names().get(db_key, {}), properties)
        if reason:
            raise CreationHeld(reason)
        payload = normalize_creation_payload(payload, {f["api_name"]: f for f in fields})
        title_field = {"client_master": "Account_Name", "chain": "Name", "product": "Product_Name", "action": "Name", "project": "Deal_Name"}[db_key]
        title = payload.get(title_field)
        if not isinstance(title, str) or not title.strip():
            raise CreationHeld("名前による重複確認を安全に行えません")
        self._check_list_duplicates(db_key, module, title_field, title)
        return payload

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

    def __init__(self, targets):
        self.targets = targets

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
        payload, _ = translate_properties(kintone_outbound_field_names(), db_key, dict(properties))
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
        payload = normalize_creation_payload(payload, fields, kintone=True)
        if db_key != "client_master":
            raise CreationHeld("一意に重複を確認する項目が未確定です")
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
