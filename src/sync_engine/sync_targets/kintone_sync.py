"""kintone向け同期ターゲット（既存業務DB。Q-01の暫定想定に基づき常時双方向同期を継続）。"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from src.db_schema.base import Tool
from src.sync_engine.outbound_field_mapping import (
    kintone_outbound_field_names,
    translate_properties,
)
from src.sync_engine.sync_targets.base import SyncTarget

logger = logging.getLogger(__name__)

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

    def get_record(self, external_id: str, *, db_key: str | None = None) -> dict[str, Any] | None:
        try:
            return self._client.get_record(self._app, external_id)
        except Exception:
            # 2026-08-27本番障害対応: 例外自体はここでは握らず呼び出し元（Dispatcher）へ
            # そのまま伝播させる（呼び出し元がスキップに倒すかどうかを判断する）。
            # ただし例外メッセージ（例: KintoneApiErrorの「HTTP 400: 不正なリクエストです。」）
            # だけでは「どのアプリ・どのレコードで失敗したか」が分からず切り分けできなかった
            # ため、レコードの中身（PII含みうる）は出さずapp/external_id/db_keyという
            # 識別子だけをここで記録する。
            logger.exception(
                "KintoneSyncTarget.get_record failed (app=%r, external_id=%r, db_key=%r)",
                self._app,
                external_id,
                db_key,
            )
            raise

    def upsert_record(
        self, external_id: str | None, properties: dict[str, Any], *, db_key: str | None = None
    ) -> str | None:
        payload = self._to_kintone_payload(properties, db_key)
        if payload is None:
            return external_id
        if external_id is None:
            return self._client.add_record(self._app, payload)
        self._client.update_record(self._app, external_id, payload)
        return external_id

    def _to_kintone_payload(
        self, properties: dict[str, Any], db_key: str | None
    ) -> dict[str, Any] | None:
        """Notionのプロパティ名をkintoneのフィールドコードへ置き換える。

        kintoneのフィールドコードは画面上のラベルと別物（「施設名（会社名）」は`店舗名`、
        「契約進捗状況」は`ドロップダウン_2`）。2026-08-31の棚卸しまで、ここには
        Notionのプロパティ名がそのまま渡っていた。詳細は
        `src/sync_engine/outbound_field_mapping.py`。
        """
        payload, unmapped = translate_properties(kintone_outbound_field_names(), db_key, properties)
        if unmapped:
            logger.warning(
                "KintoneSyncTarget: kintone側のフィールドコードが特定できないため送信しません "
                "(app=%r, db_key=%r, properties=%r)",
                self._app,
                db_key,
                unmapped,
            )
        return payload or None

    def delete_record(self, external_id: str, *, db_key: str | None = None) -> None:
        # 05_同期・競合制御「削除の扱い」：物理削除ではなく削除フラグを立てる論理削除。
        self._client.update_record(self._app, external_id, {_DELETE_FLAG_FIELD: True})
