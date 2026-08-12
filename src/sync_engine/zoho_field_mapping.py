"""Zoho CRM の api_name（内部フィールドID）→ フィールドラベル（日本語）の変換。

Zoho CRM Notifications（webhook）ペイロードの`affected_values[*].values`は、フィールドを
`api_name`（例: "field71"）で返す。一方、`src.db_schema`の`PropertyDefinition.name`（および
それをキーとするNotion/kintone/スプレッドシート横断のプロパティ名）は日本語ラベル
（例: "営業ステータス"）を使っている。この2つの命名体系の差分を埋めるための小さな
ルックアップモジュール。

マッピング自体は本モジュールでは持たず、`config/zoho_field_mapping.json`
（リポジトリルート直下、`DatabaseSchema.zoho_api_module`をトップレベルキーとする
`{module: {api_name: field_label}}`形式）に静的データとして持つ。最新化は
`scripts/fetch_zoho_field_mapping.py`（実際のZoho API `GET /crm/v3/settings/fields`から
取得）が担い、本モジュールはあくまで読み取り専用。

ファイル内容はデプロイ中に変化しない静的設定のため、`src.document_generation.google_auth`と
同様、モジュールレベル変数へロード1回だけキャッシュする（`functools.lru_cache`ではなく
明示的なグローバル変数にしているのは、`reset_cache()`でテストごとに確実にクリアできるように
するため）。
"""

from __future__ import annotations

import json
from pathlib import Path

# src/sync_engine/zoho_field_mapping.py から見て、リポジトリルート/config/ を指す。
DEFAULT_MAPPING_PATH = Path(__file__).resolve().parents[2] / "config" / "zoho_field_mapping.json"

_cached_mapping: dict[str, dict[str, str]] | None = None


def _load_mapping() -> dict[str, dict[str, str]]:
    global _cached_mapping
    if _cached_mapping is None:
        if not DEFAULT_MAPPING_PATH.exists():
            _cached_mapping = {}
        else:
            _cached_mapping = json.loads(DEFAULT_MAPPING_PATH.read_text(encoding="utf-8"))
    return _cached_mapping


def resolve_zoho_field_label(module: str, api_name: str) -> str | None:
    """Zoho CRMの`api_name`を、`DatabaseSchema.properties`のプロパティ名（日本語ラベル）へ変換する。

    以下のいずれの場合もNoneを返す（呼び出し側はどちらも「未知のフィールド」として同様に
    扱う想定のため、原因ごとに例外を分けない）。
    - `module`（`DatabaseSchema.zoho_api_module`と対応する値、例: "Deals"）がマッピング
      ファイルに存在しない
    - `api_name`がそのmodule内のマッピングに見つからない
    """
    module_mapping = _load_mapping().get(module)
    if module_mapping is None:
        return None
    return module_mapping.get(api_name)


def reset_cache() -> None:
    """モジュールレベルキャッシュを明示的にクリアする（テスト用）。"""
    global _cached_mapping
    _cached_mapping = None
