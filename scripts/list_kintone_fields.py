#!/usr/bin/env python3
"""kintoneアプリの実フィールドコード・ラベル・型の一覧を、実際のkintone REST API
（`GET /k/v1/app/form/fields.json`）から取得して表示する（読み取り専用、書き込みは一切行わない）。

■ 何のためのスクリプトか -----------------------------------------------------------------
`src.sync_engine.webhook_handlers.kintone_field_transforms.KINTONE_FIELD_TRANSFORMS`は
kintoneの実フィールドコードをキーとする変換テーブルだが、フィールドコードは表示ラベル
（CSVエクスポートの列名等）と一致しないことが多い（2026-08-14、実際にkintone→Notion方向の
Webhookを有効化した際にこの前提の誤りが発覚し、`KINTONE_FIELD_TRANSFORMS`が実質何も
拾えていなかった事故があった。`kintone_field_transforms.py`のモジュールdocstring参照）。
本スクリプトは、このテーブルを整備・再検証する際に必要な「実際のフィールドコード→
ラベル→型」の対応を毎回手作業で確認しなくて済むようにするための補助ツール。

■ 使い方 -----------------------------------------------------------------------------------
`config/.env`（または既にexport済みの環境変数）にKINTONE_DOMAIN・対象アプリの
KINTONE_API_TOKEN_*が必要（`config/.env.example`参照）。

    # 環境変数を読み込んでから実行する例
    set -a; source config/.env; set +a
    python scripts/list_kintone_fields.py --app-id "$KINTONE_APP_ID_PROJECT" \\
        --api-token "$KINTONE_API_TOKEN_PROJECT"

    # db_key名を指定して対応する環境変数から自動解決する場合
    python scripts/list_kintone_fields.py --db-key project

出力はコード・ラベル・型のみ（トークンや個別レコードの実データは一切含まない）。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_DB_KEY_ENV_VARS: dict[str, tuple[str, str]] = {
    "client_master": ("KINTONE_APP_ID_CLIENT", "KINTONE_API_TOKEN_CLIENT"),
    "project": ("KINTONE_APP_ID_PROJECT", "KINTONE_API_TOKEN_PROJECT"),
    "action": ("KINTONE_APP_ID_ACTION", "KINTONE_API_TOKEN_ACTION"),
}


def fetch_fields(domain: str, app_id: str, api_token: str) -> dict[str, dict[str, str]]:
    response = requests.get(
        f"https://{domain}/k/v1/app/form/fields.json",
        headers={"X-Cybozu-API-Token": api_token},
        params={"app": app_id},
        timeout=15,
    )
    response.raise_for_status()
    return response.json().get("properties", {})


def print_fields(db_key: str, app_id: str, fields: dict[str, dict[str, str]]) -> None:
    print(f"=== {db_key} (app_id={app_id}) ===")
    for code, field in sorted(fields.items()):
        label = field.get("label", "")
        ftype = field.get("type", "")
        marker = "" if code == label else "  <-- コード!=ラベル"
        print(f"  code={code!r} label={label!r} type={ftype!r}{marker}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--db-key",
        choices=sorted(_DB_KEY_ENV_VARS),
        help="対応する環境変数名（KINTONE_APP_ID_*/KINTONE_API_TOKEN_*）から自動解決する",
    )
    parser.add_argument("--app-id", help="kintoneアプリID（--db-key未指定時は必須）")
    parser.add_argument("--api-token", help="kintone APIトークン（--db-key未指定時は必須）")
    parser.add_argument(
        "--domain", default=os.environ.get("KINTONE_DOMAIN"), help="kintoneドメイン（既定: 環境変数KINTONE_DOMAIN）"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    if not args.domain:
        raise SystemExit("KINTONE_DOMAIN が未設定です（--domain または環境変数で指定してください）")

    db_keys = [args.db_key] if args.db_key else list(_DB_KEY_ENV_VARS)
    for db_key in db_keys:
        if args.app_id and args.api_token:
            app_id, api_token = args.app_id, args.api_token
        else:
            app_id_var, token_var = _DB_KEY_ENV_VARS[db_key]
            app_id = os.environ.get(app_id_var)
            api_token = os.environ.get(token_var)
            if not app_id or not api_token:
                print(f"{db_key}: {app_id_var}/{token_var} が未設定のためスキップ")
                continue
        fields = fetch_fields(args.domain, app_id, api_token)
        print_fields(db_key, app_id, fields)


if __name__ == "__main__":
    main()
