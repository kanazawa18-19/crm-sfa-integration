"""IDマッピングテーブル（05_同期・競合制御「レコード特定」）。

Notion主キー（CLI-xxx等）⇔ kintoneレコード番号 ⇔ Zoho ID ⇔ スプシ行 を1対1で保持し、
last_synced_at を併せて管理する。本番は DynamoDB / Firestore 想定だが、
ローカル開発・テスト用に SQLite実装を用意し、永続化層は差し替え可能にする。
"""

from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

from src.db_schema.base import Tool


@dataclass(frozen=True)
class IdMapping:
    notion_key: str
    db_key: str
    kintone_id: str | None = None
    zoho_id: str | None = None
    spreadsheet_row: int | None = None
    last_synced_at: datetime | None = None


_NOT_PROVIDED = object()  # upsertのexpected_last_synced_at未指定を表すセンチネル


class ConflictError(Exception):
    """upsertにexpected_last_synced_atを指定した際、DB上の現在値と一致しない場合の例外
    （楽観的排他制御。並行Webhook受信時のlost update検知に使う）。
    """

    def __init__(
        self, notion_key: str, expected: datetime | None, actual: datetime | None
    ) -> None:
        self.notion_key = notion_key
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"conflict on notion_key={notion_key!r}: expected last_synced_at={expected!r}, "
            f"actual={actual!r}"
        )


class DuplicateExternalIdError(Exception):
    """同一の外部ID（kintone_id/zoho_id/spreadsheet_row）が既に別のnotion_keyに
    紐づいている場合に送出する例外。find_by_external_idが先頭一致しか返せず
    もう一方のレコードが迷子になる事故を、upsert時点で明確なエラーとして検知するため。
    """

    def __init__(self, tool: Tool, external_id: str | int, existing_notion_key: str) -> None:
        self.tool = tool
        self.external_id = external_id
        self.existing_notion_key = existing_notion_key
        super().__init__(
            f"{tool.value} id {external_id!r} is already mapped to "
            f"notion_key={existing_notion_key!r}"
        )


class IdMappingStore(ABC):
    """IDマッピングテーブルの永続化層インターフェース。実装差し替え用の抽象基底クラス。"""

    @abstractmethod
    def get(self, notion_key: str) -> IdMapping | None:
        """Notion主キーからマッピングを取得する。存在しなければNoneを返す。"""

    @abstractmethod
    def upsert(
        self, mapping: IdMapping, *, expected_last_synced_at: datetime | None = _NOT_PROVIDED
    ) -> None:
        """notion_key をキーに新規作成または更新する。

        デフォルトは無条件上書きであり、並行Webhook受信時のlost update検知・回避は
        呼び出し側の責任である。expected_last_synced_at を指定した場合のみ、
        DB上の現在の last_synced_at と比較する compare-and-swap 方式で更新し、
        不一致なら ConflictError を投げる（新規作成を期待する場合は None を渡す）。
        """

    @abstractmethod
    def delete(self, notion_key: str) -> None:
        """マッピングを削除する。存在しない場合は何もしない。"""

    @abstractmethod
    def find_by_external_id(self, tool: Tool, external_id: str, *, db_key: str) -> IdMapping | None:
        """kintone/Zoho/スプレッドシートの外部IDからマッピングを逆引きする（コンフリクト検知用）。

        `db_key`は必須（2026-08-14、shirokuma-secレビューBLOCKER対応で追加）。特にkintoneの
        レコード番号はアプリ単位で独立採番されており、db_keyを無視して外部IDだけで検索すると、
        たとえば案件管理アプリのレコード#45と取引先マスタアプリのレコード#45を取り違え、
        全く別のNotionページへ誤ったプロパティを書き込んでしまう事故になりうる（実際に
        kintone→Notion方向のWebhookを有効化した際、アクション管理アプリのイベントが
        取引先マスターDBのNotionページを誤って解決し、書き込みに失敗して発覚した）。
        Zoho（IDは元々グローバルに一意）・スプレッドシート（行番号はシート単位）についても
        一貫性のため同様にdb_keyで絞り込む。
        """

    @abstractmethod
    def update_last_synced_at(self, notion_key: str, synced_at: datetime) -> None:
        """コンフリクト判定基準となる last_synced_at のみ更新する。"""

    @abstractmethod
    def list_by_db(self, db_key: str) -> list[IdMapping]:
        """指定DB（db_schema.DatabaseSchema.key）に属するマッピングを全件取得する。"""


_EXTERNAL_ID_COLUMNS: dict[Tool, str] = {
    Tool.KINTONE: "kintone_id",
    Tool.ZOHO: "zoho_id",
    Tool.SPREADSHEET: "spreadsheet_row",
}


class SQLiteIdMappingStore(IdMappingStore):
    """ローカル開発・テスト用のSQLite実装。db_path=":memory:" でインメモリ動作も可能。"""

    def __init__(self, db_path: str = ":memory:") -> None:
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS id_mapping (
                notion_key TEXT PRIMARY KEY,
                db_key TEXT NOT NULL,
                kintone_id TEXT,
                zoho_id TEXT,
                spreadsheet_row INTEGER,
                last_synced_at TEXT
            )
            """
        )
        # SQLiteのUNIQUE制約はNULLを重複扱いしない（NULL同士は複数許容）ため、
        # 未連携（NULL）のレコードを妨げずに、実際の外部ID重複のみをDBレベルでも防止できる。
        # 2026-08-14、shirokuma-secレビューBLOCKER対応: (db_key, 外部ID)の複合一意制約へ変更。
        # kintoneのレコード番号はアプリ（db_key）単位で独立採番されており、db_keyを含めない
        # 単純な一意制約だと、例えば案件管理#45と取引先マスタ#45が同じkintone_id値として
        # 衝突しうる（find_by_external_id()のdocstring参照）。
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_id_mapping_kintone_id "
            "ON id_mapping(db_key, kintone_id)"
        )
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_id_mapping_zoho_id "
            "ON id_mapping(db_key, zoho_id)"
        )
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_id_mapping_spreadsheet_row "
            "ON id_mapping(db_key, spreadsheet_row)"
        )
        self._conn.commit()

    def get(self, notion_key: str) -> IdMapping | None:
        row = self._conn.execute(
            "SELECT * FROM id_mapping WHERE notion_key = ?", (notion_key,)
        ).fetchone()
        return self._row_to_mapping(row) if row else None

    def upsert(
        self, mapping: IdMapping, *, expected_last_synced_at: datetime | None = _NOT_PROVIDED
    ) -> None:
        self._assert_no_duplicate_external_id(mapping)
        if expected_last_synced_at is not _NOT_PROVIDED:
            current = self.get(mapping.notion_key)
            current_synced_at = current.last_synced_at if current else None
            if current_synced_at != expected_last_synced_at:
                raise ConflictError(mapping.notion_key, expected_last_synced_at, current_synced_at)
        try:
            self._conn.execute(
                """
                INSERT INTO id_mapping
                    (notion_key, db_key, kintone_id, zoho_id, spreadsheet_row, last_synced_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(notion_key) DO UPDATE SET
                    db_key = excluded.db_key,
                    kintone_id = excluded.kintone_id,
                    zoho_id = excluded.zoho_id,
                    spreadsheet_row = excluded.spreadsheet_row,
                    last_synced_at = excluded.last_synced_at
                """,
                (
                    mapping.notion_key,
                    mapping.db_key,
                    mapping.kintone_id,
                    mapping.zoho_id,
                    mapping.spreadsheet_row,
                    mapping.last_synced_at.isoformat() if mapping.last_synced_at else None,
                ),
            )
            self._conn.commit()
        except sqlite3.IntegrityError as exc:
            # 事前チェック（_assert_no_duplicate_external_id）と実INSERTの間の競合など、
            # UNIQUE制約側で最終的に検知したケースのフォールバック。
            self._conn.rollback()
            raise DuplicateExternalIdError(
                Tool.KINTONE, mapping.kintone_id or mapping.zoho_id or mapping.spreadsheet_row or "",
                mapping.notion_key,
            ) from exc

    def _assert_no_duplicate_external_id(self, mapping: IdMapping) -> None:
        """外部ID（kintone_id/zoho_id/spreadsheet_row）が同一db_key内で既に別のnotion_keyに
        紐づいていないか検査する（db_keyをまたいだ重複は正当なので許容する。例えば案件管理の
        レコード#45と取引先マスタのレコード#45が同じkintone_id="45"を持つのは異なるdb_key
        なので問題ない）。
        """
        for tool, value in (
            (Tool.KINTONE, mapping.kintone_id),
            (Tool.ZOHO, mapping.zoho_id),
            (Tool.SPREADSHEET, mapping.spreadsheet_row),
        ):
            if value is None:
                continue
            existing = self.find_by_external_id(tool, str(value), db_key=mapping.db_key)
            if existing is not None and existing.notion_key != mapping.notion_key:
                raise DuplicateExternalIdError(tool, value, existing.notion_key)

    def delete(self, notion_key: str) -> None:
        self._conn.execute("DELETE FROM id_mapping WHERE notion_key = ?", (notion_key,))
        self._conn.commit()

    def find_by_external_id(self, tool: Tool, external_id: str, *, db_key: str) -> IdMapping | None:
        column = _EXTERNAL_ID_COLUMNS.get(tool)
        if column is None:
            raise ValueError(f"unsupported tool for external id lookup: {tool}")
        value: str | int = int(external_id) if tool is Tool.SPREADSHEET else external_id
        row = self._conn.execute(
            f"SELECT * FROM id_mapping WHERE {column} = ? AND db_key = ?",  # noqa: S608 (columnは固定辞書のみ)
            (value, db_key),
        ).fetchone()
        return self._row_to_mapping(row) if row else None

    def update_last_synced_at(self, notion_key: str, synced_at: datetime) -> None:
        cursor = self._conn.execute(
            "UPDATE id_mapping SET last_synced_at = ? WHERE notion_key = ?",
            (synced_at.isoformat(), notion_key),
        )
        self._conn.commit()
        if cursor.rowcount == 0:
            raise KeyError(f"no id_mapping found for notion_key={notion_key!r}")

    def list_by_db(self, db_key: str) -> list[IdMapping]:
        rows = self._conn.execute(
            "SELECT * FROM id_mapping WHERE db_key = ?", (db_key,)
        ).fetchall()
        return [self._row_to_mapping(row) for row in rows]

    def close(self) -> None:
        self._conn.close()

    @staticmethod
    def _row_to_mapping(row: sqlite3.Row) -> IdMapping:
        return IdMapping(
            notion_key=row["notion_key"],
            db_key=row["db_key"],
            kintone_id=row["kintone_id"],
            zoho_id=row["zoho_id"],
            spreadsheet_row=row["spreadsheet_row"],
            last_synced_at=(
                datetime.fromisoformat(row["last_synced_at"]) if row["last_synced_at"] else None
            ),
        )
