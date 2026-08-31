"""Zohoの選択肢一覧を `config/zoho_picklists.json` へ書き出す（2026-08-31）。

Notion→Zohoの書き込みで、選択肢の値を読み替えるために使う
（`src/sync_engine/outbound_value_mapping.py`）。

**選択肢はZoho側で増減する。** 増えたときに気づけるよう、対応表を手で書かずに
このスナップショットから機械的に導く方針にしている（`config/zoho_field_mapping.json`と
同じ考え方）。Zohoで選択肢を足したら、このスクリプトを流し直して差分を確認すること。

    python scripts/fetch_zoho_picklists.py
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from src.db_schema.registry import ALL_SCHEMAS
from src.sync_engine.production_wiring import build_zoho_client

logger = logging.getLogger("fetch_zoho_picklists")

OUTPUT_PATH = Path(__file__).resolve().parents[1] / "config" / "zoho_picklists.json"


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    client = build_zoho_client()
    if client is None:
        logger.error("Zohoクライアントを構築できません（認証情報を確認してください）")
        return 1

    result: dict[str, dict[str, dict[str, object]]] = {}
    for module in sorted({schema.zoho_api_module for schema in ALL_SCHEMAS}):
        response = client._request("GET", f"/settings/fields?module={module}")  # noqa: SLF001
        fields = response.json().get("fields", [])
        picks: dict[str, dict[str, object]] = {}
        for field in fields:
            options = [
                value["display_value"]
                for value in (field.get("pick_list_values") or [])
                if value.get("display_value")
            ]
            if options:
                picks[field["api_name"]] = {
                    "label": field.get("field_label"),
                    "options": options,
                }
        result[module] = picks
        logger.info("%s: 選択肢を持つ項目 %d個", module, len(picks))

    OUTPUT_PATH.write_text(
        json.dumps(result, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    logger.info("書き出しました: %s", OUTPUT_PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
