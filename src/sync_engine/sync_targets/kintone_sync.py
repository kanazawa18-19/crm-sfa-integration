"""kintone向け同期ターゲット（既存業務DB。Q-01の暫定想定に基づき常時双方向同期を継続）。"""

from __future__ import annotations

from typing import Any, Protocol

from src.db_schema.base import Tool
from src.sync_engine.sync_targets.base import SyncTarget

_DELETE_FLAG_FIELD = "削除フラグ"


class KintoneClient(Protocol):
    """kintone REST APIの最小インターフェース。実HTTP通信は本Protocolの実装側が担う。"""

    def get_record(self, app: str, record_id: str) -> dict[str, Any] | None: ...

    def add_record(self, app: str, record: dict[str, Any]) -> str:
        """レコードを新規登録し、採番されたレコード番号を返す。"""
        ...

    def update_record(self, app: str, record_id: str, record: dict[str, Any]) -> None: ...


class KintoneSyncTarget(SyncTarget):
    """kintoneはDB（取引先マスタ/案件管理/アクション管理）ごとにアプリが分かれるため、
    appはDB単位でインスタンス化時に固定する。
    """

    tool = Tool.KINTONE

    def __init__(self, client: KintoneClient, app: str) -> None:
        self._client = client
        self._app = app

    def get_record(self, external_id: str) -> dict[str, Any] | None:
        return self._client.get_record(self._app, external_id)

    def upsert_record(self, external_id: str | None, properties: dict[str, Any]) -> str:
        if external_id is None:
            return self._client.add_record(self._app, properties)
        self._client.update_record(self._app, external_id, properties)
        return external_id

    def delete_record(self, external_id: str) -> None:
        # 05_同期・競合制御「削除の扱い」：物理削除ではなく削除フラグを立てる論理削除。
        self._client.update_record(self._app, external_id, {_DELETE_FLAG_FIELD: True})
