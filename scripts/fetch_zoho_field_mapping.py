#!/usr/bin/env python3
"""Zoho CRMの指定モジュールについて、api_name（内部フィールドID）→フィールドラベル（日本語）の
マッピングを実際のZoho API（`GET /crm/v3/settings/fields?module={module}`）から取得し、
`config/zoho_field_mapping.json`の該当モジュール部分のみを更新するスクリプト。

■ 何のためのマッピングか -------------------------------------------------------------------
Zoho CRM Notifications（webhook）ペイロードの`affected_values[*].values`はフィールドを
`api_name`（例: "field71"）で返すが、`src.db_schema`側のプロパティ名は日本語ラベル
（例: "営業ステータス"）を使っている。この差分を埋めるのが`config/zoho_field_mapping.json`
（`src.sync_engine.zoho_field_mapping.resolve_zoho_field_label()`が読み取り専用で参照する）で、
本スクリプトはその中身を最新化する手段。新しいカスタムフィールドがZoho側に追加された時や、
Deals以外の対象モジュール（`src/db_schema/registry.py`の`ALL_SCHEMAS`各`zoho_api_module`）の
マッピングを新たに用意したい時に実行する。

■ このスクリプトが行うこと ---------------------------------------------------------------------
1. `src.sync_engine.clients.zoho_client.HttpZohoClient`
   （`src.sync_engine.zoho_watch_channel.build_zoho_client_from_env()`経由、認証は
   `ZOHO_CLIENT_ID`/`ZOHO_CLIENT_SECRET`/`ZOHO_REFRESH_TOKEN`から）を使って対象moduleの
   フィールド定義（`GET /crm/v3/settings/fields?module={module}`）を取得する。
2. 取得結果（`api_name -> field_label`）と、`config/zoho_field_mapping.json`に既存の
   同モジュールのマッピングとを比較し、追加/削除/ラベル変更の差分を表示する。
3. 差分表示の後、対象moduleのセクションのみを新しい内容へ置き換えてファイルへ書き込む
   （他モジュールの既存セクションはそのまま保持する）。

使い方:
    # Deals（既定）モジュールのマッピングを取得・更新
    python scripts/fetch_zoho_field_mapping.py

    # 他モジュール（例: 取引先マスタに対応するカスタムモジュール）を指定
    python scripts/fetch_zoho_field_mapping.py --module CustomModule1

本番Zoho orgは.jpデータセンター所属のため、`settings/fields` APIのベースURLも
`src.sync_engine.zoho_watch_channel.DEFAULT_WATCH_API_BASE_URL`と同じ`.jp`ドメインを既定にする
（`--api-base-url`で上書き可能）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.sync_engine.clients._http import raise_for_error
from src.sync_engine.clients.zoho_client import HttpZohoClient, ZohoApiError
from src.sync_engine.zoho_field_mapping import DEFAULT_MAPPING_PATH
from src.sync_engine.zoho_watch_channel import build_zoho_client_from_env

# zoho_watch_channel.DEFAULT_WATCH_API_BASE_URLと同じ理由（本番Zoho orgは.jpデータセンター
# 所属）。settings系APIも watch系と同じ `/crm/v3` 配下。
DEFAULT_SETTINGS_API_BASE_URL = "https://www.zohoapis.jp/crm/v3"
DEFAULT_MODULE = "Deals"


def fetch_module_field_mapping(
    client: HttpZohoClient, *, module: str, settings_api_base_url: str
) -> dict[str, str]:
    """`GET /crm/v3/settings/fields?module={module}`からapi_name -> field_labelを取得する。

    `HttpZohoClient.request()`は`params`引数を持たない（watch APIのような絶対URL渡し専用の
    ため、クエリ文字列は呼び出し側が組み立てる想定）。ここでも同様にmoduleをクエリ文字列へ
    自前でエンコードしてURLへ含める。
    """
    query = urlencode({"module": module})
    url = f"{settings_api_base_url.rstrip('/')}/settings/fields?{query}"
    response = client.request("GET", url, idempotent=True)
    raise_for_error(response, ZohoApiError)

    try:
        body = response.json()
    except ValueError as exc:
        raise ZohoApiError(
            response.status_code, "zoho settings/fields api response body was not valid JSON"
        ) from exc
    fields = body.get("fields") if isinstance(body, dict) else None
    if not isinstance(fields, list):
        raise ZohoApiError(
            response.status_code,
            "zoho settings/fields api response did not include a 'fields' array",
        )

    mapping: dict[str, str] = {}
    for entry in fields:
        if not isinstance(entry, dict):
            continue
        api_name = entry.get("api_name")
        label = entry.get("field_label")
        if isinstance(api_name, str) and isinstance(label, str):
            mapping[api_name] = label
    return mapping


def load_full_mapping(path: Path) -> dict[str, dict[str, str]]:
    """既存の`config/zoho_field_mapping.json`全体を読み込む（無ければ空dict）。"""
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def diff_module_mapping(
    old: dict[str, str], new: dict[str, str]
) -> dict[str, Any]:
    """1モジュール分の旧マッピングと新マッピングを比較する。

    - added: 新規に出現したapi_name
    - removed: 無くなった（Zoho側で削除された、または取得結果に含まれなくなった）api_name
    - changed: api_nameは同じだがfield_label（表示名）が変わったもの（Zoho側でのフィールド
      名称変更を指す。api_nameそのものが変わるケースはZoho側の仕様上別フィールド扱いとなり
      added/removedとして表れる）
    """
    added = {k: v for k, v in new.items() if k not in old}
    removed = {k: v for k, v in old.items() if k not in new}
    changed = {k: (old[k], new[k]) for k in new if k in old and old[k] != new[k]}
    unchanged_count = len(new) - len(added) - len(changed)
    return {"added": added, "removed": removed, "changed": changed, "unchanged_count": unchanged_count}


def print_diff_summary(module: str, diff: dict[str, Any]) -> None:
    print(f"=== {module} フィールドマッピング更新 ===")
    added, removed, changed = diff["added"], diff["removed"], diff["changed"]
    if not added and not removed and not changed:
        print("変更なし")
    if added:
        print(f"追加: {len(added)}件")
        for api_name, label in sorted(added.items()):
            print(f"  + {api_name}: {label}")
    if removed:
        print(f"削除: {len(removed)}件")
        for api_name, label in sorted(removed.items()):
            print(f"  - {api_name}: {label}")
    if changed:
        print(f"ラベル変更: {len(changed)}件")
        for api_name, (old_label, new_label) in sorted(changed.items()):
            print(f"  ~ {api_name}: {old_label} -> {new_label}")
    print(f"変更なし（既存のまま）: {diff['unchanged_count']}件")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--module",
        default=DEFAULT_MODULE,
        help=f"対象Zohoモジュール（`DatabaseSchema.zoho_api_module`と一致させる。既定: {DEFAULT_MODULE}）",
    )
    parser.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_MAPPING_PATH,
        help=f"更新対象のマッピングファイル（既定: {DEFAULT_MAPPING_PATH}）",
    )
    parser.add_argument(
        "--api-base-url",
        default=DEFAULT_SETTINGS_API_BASE_URL,
        help=f"settings/fields APIのベースURL（既定: {DEFAULT_SETTINGS_API_BASE_URL}）",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    client = build_zoho_client_from_env()
    new_module_mapping = fetch_module_field_mapping(
        client, module=args.module, settings_api_base_url=args.api_base_url
    )

    full_mapping = load_full_mapping(args.path)
    old_module_mapping = full_mapping.get(args.module, {})
    diff = diff_module_mapping(old_module_mapping, new_module_mapping)
    print_diff_summary(args.module, diff)

    full_mapping[args.module] = new_module_mapping
    args.path.write_text(
        json.dumps(full_mapping, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"\n{args.path} を更新しました（{args.module}: {len(new_module_mapping)}フィールド）。")


if __name__ == "__main__":
    main()
