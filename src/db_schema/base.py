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

    @property
    def is_required(self) -> bool:
        return self.requirement == RequirementLevel.REQUIRED

    @property
    def is_auto(self) -> bool:
        return self.requirement == RequirementLevel.AUTO

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
    # 移行元Zohoモジュール名（例:「案件」）。kintone_key同様、Notion側の値ではなく
    # 移行元システムの参照名。
    zoho_key: str
    properties: tuple[PropertyDefinition, ...]

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
