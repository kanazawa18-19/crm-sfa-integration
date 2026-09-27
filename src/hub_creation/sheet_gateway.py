"""新規登録メニューが行へ付けた識別情報で追跡する。行番号で更新しない。"""
from src.hub_creation.domain import CreationHeld
from src.sync_engine.clients._http import raise_for_error, ApiError

METADATA_KEY = "CRM_NEW_REGISTRATION"


class SheetRegistrationGateway:
    def __init__(self, client):
        self.client = client

    def _post(self, path, body):
        response = self.client._request("POST", path, json_body=body)
        raise_for_error(response, ApiError)
        return response.json()

    def read(self, sheet, key, accepted_key=None):
        data = self._post("/developerMetadata:search", {"dataFilters": [{"developerMetadataLookup": {
            "metadataKey": METADATA_KEY, "metadataValue": key, "visibility": "DOCUMENT"}}]})
        matches = data.get("matchedDeveloperMetadata", [])
        if len(matches) != 1:
            raise CreationHeld("新規登録行の識別情報が無いか重複しています")
        metadata = matches[0]["developerMetadata"]
        location = metadata.get("location", {}).get("dimensionRange", {})
        if location.get("dimension") != "ROWS" or location.get("endIndex", 0) - location.get("startIndex", 0) != 1:
            raise CreationHeld("新規登録行の識別情報が不正です")
        if location.get("sheetId") != self.client._grid_properties(sheet)["sheetId"]:
            raise CreationHeld("新規登録行が別のシートへ移動しています")
        lookup = {"developerMetadataLookup": {"metadataId": metadata["metadataId"]}}
        response = self._post("/values:batchGetByDataFilter", {"dataFilters": [lookup], "valueRenderOption": "UNFORMATTED_VALUE", "dateTimeRenderOption": "SERIAL_NUMBER"})
        ranges = response.get("valueRanges", [])
        if len(ranges) != 1:
            raise CreationHeld("新規登録行を一意に読めません")
        rows = ranges[0]["valueRange"].get("values", [])
        if len(rows) != 1:
            raise CreationHeld("新規登録行を一意に読めません")
        headers = self.client._get_header_row(sheet)
        values = dict(zip(headers, rows[0]))
        if values.get("同期キー") not in {value for value in (key, accepted_key) if value}:
            raise CreationHeld("登録中の同期キーが変更されています")
        # 複製キーはmetadataが1つでも許可しない。
        if self.client.find_unique_row_by_sync_key(sheet, "同期キー", values.get("同期キー")) is None:
            raise CreationHeld("同期キーが無いか複製されています")
        return values, metadata["metadataId"], location["startIndex"] + 1

    def update(self, sheet, key, *, notion_key=None, status):
        _values, metadata_id, row = self.read(sheet, key, accepted_key=notion_key)
        headers = self.client._get_header_row(sheet)
        values = [None] * len(headers)
        if notion_key is not None:
            values[headers.index("同期キー")] = notion_key
        if "登録状況" in headers:
            values[headers.index("登録状況")] = status
        self._post("/values:batchUpdateByDataFilter", {"valueInputOption": "RAW", "data": [{
            "dataFilter": {"developerMetadataLookup": {"metadataId": metadata_id}}, "majorDimension": "ROWS", "values": [values]}]})
        return row
