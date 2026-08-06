"""Notion 6DB共通のスキーマ定義プリミティブ。

03_プロパティ定義の「同期対象」列（全ツール／Notionのみ／スプシのみ／内部）を
同期エンジンがプロパティ単位で判定できるよう、SyncScope として型に落とし込む。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Tool(str, Enum):
    """01_システム構成に登場する外部連携ツール。"""

    NOTION = "notion"
    SPREADSHEET = "spreadsheet"
    KINTONE = "kintone"
    ZOHO = "zoho"


class PropertyType(str, Enum):
    TITLE = "title"
    TEXT = "text"
    SELECT = "select"
    STATUS = "status"
    MULTI_SELECT = "multi_select"
    RELATION = "relation"
    NUMBER = "number"
    CURRENCY = "currency"
    DATE = "date"
    DATETIME = "datetime"
    EMAIL = "email"
    PHONE = "phone"
    URL = "url"
    CHECKBOX = "checkbox"
    USER = "user"
    JSON_TEXT = "json_text"
    # 以下はNotion APIから一切書き込めない読み取り専用型（PropertyDefinition.is_writable参照）。
    ROLLUP = "rollup"
    FORMULA = "formula"
    BUTTON = "button"
    UNIQUE_ID = "unique_id"
    CREATED_TIME = "created_time"
    LAST_EDITED_TIME = "last_edited_time"
    CREATED_BY = "created_by"
    FILES = "files"


# Notion APIが値の書き込みを受け付けない型（サーバー側で自動計算・自動設定される）。
READ_ONLY_PROPERTY_TYPES: frozenset[PropertyType] = frozenset(
    {
        PropertyType.ROLLUP,
        PropertyType.FORMULA,
        PropertyType.BUTTON,
        PropertyType.UNIQUE_ID,
        PropertyType.CREATED_TIME,
        PropertyType.LAST_EDITED_TIME,
        PropertyType.CREATED_BY,
    }
)


class RequirementLevel(str, Enum):
    """03_プロパティ定義の「必須／自動」列。未指定（任意）も含めて3値で表現する。"""

    REQUIRED = "required"
    OPTIONAL = "optional"
    AUTO = "auto"


# 各SyncScopeが実際にどの外部ツールへ伝播するかのテーブル。
# Notionは常にプロパティの保持元なのでここには含めない。
_SYNC_SCOPE_TOOLS: dict["SyncScope", frozenset[Tool]] = {}


class SyncScope(str, Enum):
    """03_プロパティ定義の「同期対象」列。"""

    ALL_TOOLS = "all_tools"
    NOTION_ONLY = "notion_only"
    SPREADSHEET_ONLY = "spreadsheet_only"
    INTERNAL = "internal"

    @property
    def synced_tools(self) -> frozenset[Tool]:
        """このプロパティを反映すべき外部ツール（Notion以外）の集合。"""
        return _SYNC_SCOPE_TOOLS[self]

    def includes(self, tool: Tool) -> bool:
        return tool in self.synced_tools


_SYNC_SCOPE_TOOLS.update(
    {
        SyncScope.ALL_TOOLS: frozenset({Tool.SPREADSHEET, Tool.KINTONE, Tool.ZOHO}),
        SyncScope.NOTION_ONLY: frozenset(),
        SyncScope.SPREADSHEET_ONLY: frozenset({Tool.SPREADSHEET}),
        SyncScope.INTERNAL: frozenset(),
    }
)


@dataclass(frozen=True)
class PropertyDefinition:
    name: str
    property_type: PropertyType
    requirement: RequirementLevel
    sync_scope: SyncScope
    description: str = ""
    options: tuple[str, ...] = ()
    # RELATION型の場合の参照先DB（DatabaseSchema.key）。自DB参照（セルフリレーション）も許容する。
    relation_target: str | None = None

    def __post_init__(self) -> None:
        if self.property_type == PropertyType.RELATION and not self.relation_target:
            raise ValueError(f"relation property '{self.name}' must set relation_target")
        # 読み取り専用型はNotion API側から書き込めないため、同期エンジンの書き込み対象に
        # ならないことを型システムで保証する（INTERNAL以外のscopeは矛盾）。
        if self.property_type in READ_ONLY_PROPERTY_TYPES and self.sync_scope != SyncScope.INTERNAL:
            raise ValueError(
                f"read-only property '{self.name}' ({self.property_type.value}) "
                f"must use sync_scope=SyncScope.INTERNAL"
            )

    @property
    def is_required(self) -> bool:
        return self.requirement == RequirementLevel.REQUIRED

    @property
    def is_auto(self) -> bool:
        return self.requirement == RequirementLevel.AUTO

    @property
    def is_writable(self) -> bool:
        """Notion APIから値を書き込める型かどうか。"""
        return self.property_type not in READ_ONLY_PROPERTY_TYPES

    def should_sync_to(self, tool: Tool) -> bool:
        return self.sync_scope.includes(tool)


def common_internal_properties() -> tuple[PropertyDefinition, ...]:
    """全DB共通の内部管理プロパティ。

    04_項目マッピングの「全モジュール | 作成日時 / 更新日時 | 内部」行と、
    05_同期・競合制御のコンフリクト判定（updated_at > last_synced_at）が
    全DB・全レコード単位で必要なため、DBごとに個別定義せず共通化する。
    """
    return (
        PropertyDefinition(
            name="created_at",
            property_type=PropertyType.DATETIME,
            requirement=RequirementLevel.AUTO,
            sync_scope=SyncScope.INTERNAL,
            description="レコード作成日時",
        ),
        PropertyDefinition(
            name="updated_at",
            property_type=PropertyType.DATETIME,
            requirement=RequirementLevel.AUTO,
            sync_scope=SyncScope.INTERNAL,
            description="レコード更新日時。コンフリクト判定（updated_at > last_synced_at）に使用",
        ),
        PropertyDefinition(
            name="last_synced_at",
            property_type=PropertyType.DATETIME,
            requirement=RequirementLevel.AUTO,
            sync_scope=SyncScope.INTERNAL,
            description="同期制御用。コンフリクト判定の基準",
        ),
    )


@dataclass(frozen=True)
class DatabaseSchema:
    key: str
    display_name: str
    id_prefix: str
    # 移行元kintoneアプリ側のフィールド名／アプリ名（例:「案件管理 レコード番号」）。
    # Notion側に保持する外部ID値そのものではない点に注意（後者は各DBの
    # `kintone_ID`/`kintone_Act_ID` 等のPropertyDefinitionが担う）。
    kintone_key: str
    # 移行元Zohoモジュール名の日本語ラベル（例:「案件」）。あくまで移行元システムの参照名の
    # 表示用ラベルであり、実際のZoho CRM APIの module 値（英語API名）とは別物。
    # Webhookペイロードのmodule値の逆引きには zoho_api_module を使うこと。
    zoho_key: str
    # 実際のZoho CRM APIモジュール名（例: "Deals"）。標準モジュールに対応が無いDBは
    # カスタムモジュール名のプレースホルダ（"CustomModule1" 等）を割り当てている。
    # zoho_webhook.py の module ↔ db_key 逆引きに使用する。
    zoho_api_module: str
    # 対応するGoogleスプレッドシートのタブ名（例:「案件管理」）。spreadsheet_webhook.py の
    # sheet ↔ db_key 逆引きに使用する。display_nameから機械的に導出すると表示名変更で
    # 壊れるため、明示的なフィールドとして保持する。
    spreadsheet_sheet_name: str
    properties: tuple[PropertyDefinition, ...]
    # 既存の稼働中Notionワークスペースに実在するDBのdatabase_id（Notion API
    # `GET /v1/databases/{id}` で取得済みの固定値）。migrate_data.py・notion_webhook.py
    # はこの値をALL_SCHEMAS経由で直接参照する（旧scripts/.notion_db_ids.jsonキャッシュは廃止済み）。
    notion_database_id: str | None = None

    def __post_init__(self) -> None:
        titles = [p for p in self.properties if p.property_type == PropertyType.TITLE]
        if len(titles) != 1:
            raise ValueError(f"{self.key}: exactly one TITLE property is required, got {len(titles)}")
        names = [p.name for p in self.properties]
        if len(names) != len(set(names)):
            raise ValueError(f"{self.key}: duplicate property names detected")

    @property
    def title_property(self) -> PropertyDefinition:
        return next(p for p in self.properties if p.property_type == PropertyType.TITLE)

    def get_property(self, name: str) -> PropertyDefinition:
        for prop in self.properties:
            if prop.name == name:
                return prop
        raise KeyError(f"{self.key}: property '{name}' not found")

    def properties_synced_to(self, tool: Tool) -> list[PropertyDefinition]:
        return [p for p in self.properties if p.should_sync_to(tool)]
