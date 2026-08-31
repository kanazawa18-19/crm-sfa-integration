"""4ツール共通の同期ターゲットインターフェース（01_システム構成「疎結合設計」）。

実際のHTTP通信を行うクライアント部分（各tool別モジュールのXxxClient Protocol）と、
ロジック部分（本インターフェースを実装するXxxSyncTarget）を分離し、テストでは
実APIキー無しでモッククライアントを注入できるようにする。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from src.db_schema.base import Tool


class SyncTarget(ABC):
    """ツール別同期モジュール（notion_sync/spreadsheet_sync/kintone_sync/zoho_sync）の共通契約。"""

    tool: Tool

    @abstractmethod
    def get_record(self, external_id: str, *, db_key: str | None = None) -> dict[str, Any] | None:
        """外部ID（各ツール固有のレコードID）でレコードを取得する。存在しなければNoneを返す。

        `db_key`はDB単位に構築済みの実装（`KintoneSyncTarget`/`ZohoSyncTarget`/
        `SpreadsheetSyncTarget`）では無視してよい。`production_wiring.py`の
        `_MultiDbKintoneSyncTarget`等（外部IDから対象DBのインスタンスをルーティングする
        ラッパー）でのみ必須（2026-08-14、shirokuma-secレビューBLOCKER対応で追加。
        kintoneのレコード番号はアプリ単位で独立採番されており、db_key無しのルーティングでは
        別アプリの同番号レコードを取り違えうるため）。
        """

    def unsupported_properties(
        self, properties: dict[str, Any], *, db_key: str | None = None
    ) -> frozenset[str]:
        """このツールへ送れないプロパティ名を返す（既定は「全部送れる」）。

        `Dispatcher`は**1イベント・1ツールにつき1回**にまとめて書き込む。
        まとめると戻り値だけでは「どの項目が落ちたか」が分からないので、
        書く前にここで聞く（2026-09-01）。

        項目名や値の対応が無くて送れないツール（kintone・Zoho）が実装する。
        Notion・スプレッドシートは受け取った値をそのまま書けるので既定のままでよい。
        """
        return frozenset()

    @abstractmethod
    def upsert_record(
        self,
        external_id: str | None,
        properties: dict[str, Any],
        *,
        db_key: str | None = None,
        expected_version: str | None = None,
    ) -> str | None:
        """レコードを作成または更新し、外部IDを返す。

        external_id が None の場合は新規作成（採番されたIDを返す）、
        それ以外は更新（渡されたexternal_idをそのまま返す）。

        ツールが無効化されている等の理由でレコードが実際には作成・更新されなかった場合は
        Noneを返すこと（例: ZohoSyncTargetのENABLE_ZOHO=False時）。空文字列""を返すと
        採番済みIDと誤認されうるため使わない。

        `db_key`は`get_record`と同じ理由・同じ例外（DB単位実装では無視してよい、
        `_MultiDbXSyncTarget`でのみ必須）。
        """

    @abstractmethod
    def delete_record(self, external_id: str, *, db_key: str | None = None) -> None:
        """レコードを削除する。

        05_同期・競合制御「削除の扱い」に従い、物理削除ではなく削除フラグによる
        論理削除として実装すること（誤操作が4ツールへ即時波及するリスクの回避）。

        `db_key`は`get_record`と同じ理由・同じ例外。
        """
