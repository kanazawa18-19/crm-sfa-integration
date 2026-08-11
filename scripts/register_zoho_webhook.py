#!/usr/bin/env python3
"""Zoho CRM Notifications（watch）APIへ、本デプロイのZoho Webhook受信エンドポイント
（`POST /api/webhooks/zoho`, `src/sync_engine/webhook_handlers/zoho_webhook.py`）を
購読登録／更新（延長）するスクリプト。

対象は `Deals` モジュール（`src/db_schema/project.py` の `PROJECT_SCHEMA.zoho_api_module`
と一致させる）。認証は `src/sync_engine/clients/zoho_client.py` の `HttpZohoClient` を再利用し、
`ZOHO_CLIENT_ID`/`ZOHO_CLIENT_SECRET`/`ZOHO_REFRESH_TOKEN` からのトークンリフレッシュ・
キャッシュをそのまま流用する（本スクリプト独自の認証パスは持たない）。

■ Zoho通知の認証はbody内`token`フィールド方式（解決済み） -----------------------------------
Zoho CRM Notifications（watch）APIのリクエストスキーマ（`POST /crm/v3/actions/watch`）は、
登録時に任意のHTTPヘッダーをZoho側の送信リクエストへ付与させる仕組みを持たない。代わりに
`token` という文字列フィールドを登録時に指定でき、Zohoは通知を送る際にこの値を**通知ペイロード
のJSON body内の`token`キー**としてそのまま返してくる（HTTPヘッダーとしてではない）。

このため、`src/sync_engine/webhook_handlers/zoho_webhook.py`の`handler()`は他ハンドラの
`verify_webhook_secret()`（`X-Webhook-Secret`ヘッダー方式）ではなく、`_common.py`の
`verify_webhook_body_token()`（bodyの`token`フィールドを`ZOHO_WEBHOOK_SECRET`と
`hmac.compare_digest`で照合、fail-closed）を使う。本スクリプトが`--token`のデフォルトとして
渡す`ZOHO_WEBHOOK_SECRET`と、受信側が検証に使う`ZOHO_WEBHOOK_SECRET`は同じ環境変数なので、
両者は自動的に一致する。

  ※ Zoho側の挙動理解はZoho CRM API v3 Notifications公式ドキュメントの記載内容に基づくが、
    本タスクでは実際に本番APIへ登録して挙動を確認すること自体が禁止されているため未検証。
    実際に有効化する前に、Zoho公式ドキュメント
    （https://www.zoho.com/crm/developer/docs/api/v3/notifications.html）
    で最新のリクエスト/レスポンス仕様を再確認すること。

■ このスクリプトが行うこと ---------------------------------------------------------------------
1. `Deals` モジュール向けのwatchペイロード（channel_id/events/channel_expiry/notify_url/token）を
   組み立てて表示する（常に実行される。dry-run表示）。
2. `--channel-id` を指定した場合は既存チャンネルの更新（延長）としてPUT、指定しない場合は
   前回`--yes`成功時に保存された`.zoho_watch_channel.json`のchannel_idがあればそれを延長対象
   として使う（無ければ新規登録としてPOST、channel_idは自動生成する）。
3. `--yes` を明示的に指定した場合のみ、実際にZoho APIへリクエストを送る。指定しない限り
   表示のみで終了する（本タスクでは`--yes`を付けての実行自体を行わないこと。ユーザーが
   別途、本番Vercel環境へのZOHO_WEBHOOK_SECRET設定を確認した上で実行する）。
4. BLOCKER3対策: `--yes`指定時にtoken（既定は環境変数`ZOHO_WEBHOOK_SECRET`、`--token`で上書き可）
   が空の場合は、`--allow-empty-token`を明示しない限り登録を拒否する（実際のAPI呼び出しは
   行わない）。ローカルシェルでのZOHO_WEBHOOK_SECRET未設定・Vercel本番との食い違いによる
   「登録済みだが受信側で全通知401拒否される」事故を防ぐため。
5. BLOCKER1対策: dry-run表示・登録成功後のレスポンス表示は、いずれもtokenフィールドを
   `***REDACTED***`に伏せたコピーのみを標準出力へ出す（実際にAPIへ送るpayload自体は変更しない）。
6. WARN4対策: `--yes`成功後、channel_id/channel_expiryを`.zoho_watch_channel.json`
   （リポジトリ直下、.gitignore対象）へ保存し、`ZOHO_WATCH_CHANNEL_ID=... ZOHO_WATCH_EXPIRY=...`
   という1行もあわせて出力する（ターミナル出力が失われても次回実行時に延長対象を復元できるように）。

■ cron等への配線は本タスクのスコープ外。activation確認後の別タスクで対応する。

使い方:
    # dry-run（常定。何が送られるかを確認するだけ）
    python scripts/register_zoho_webhook.py --base-url https://crm-sfa-integration.vercel.app

    # 既存チャンネルの延長（channel_idは前回登録時のレスポンス/ログから取得したもの）
    python scripts/register_zoho_webhook.py --base-url https://crm-sfa-integration.vercel.app \\
        --channel-id 1000000026001

    # 実際に登録/更新する（要 ZOHO_CLIENT_ID/ZOHO_CLIENT_SECRET/ZOHO_REFRESH_TOKEN、および
    # ローカルシェルでVercel本番と同じ値をexportしたZOHO_WEBHOOK_SECRET。BLOCKER3対策により、
    # tokenが空のままだと--yesは拒否される）
    export ZOHO_WEBHOOK_SECRET=<Vercel本番に設定した値と同じもの>
    python scripts/register_zoho_webhook.py --base-url https://crm-sfa-integration.vercel.app --yes
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.sync_engine.clients.zoho_client import HttpZohoClient, ZohoApiError
from src.sync_engine.clients._http import raise_for_error

_DEFAULT_MODULE = "Deals"
_DEFAULT_EXPIRY_DAYS = 7
# 本番Zoho orgは.jpデータセンター所属（ZOHO_ACCOUNTS_BASE_URL/ZOHO_API_BASE_URLと同じ理由）。
# HttpZohoClientのCRUD用api_base_urlは`/crm/v2`固定だが、watch(Notifications) APIは`/crm/v3`。
_DEFAULT_WATCH_API_BASE_URL = "https://www.zohoapis.jp/crm/v3"
# WARN4: 登録成功時のchannel_id/channel_expiryを控えておくローカルファイル（リポジトリ直下）。
# 次回実行時に--channel-id省略時のデフォルト（延長対象）として読み戻す。PIIは含まないが
# 運用メタ情報のためリポジトリにはコミットしない（.gitignore参照）。
_CHANNEL_STATE_PATH = Path(__file__).resolve().parent.parent / ".zoho_watch_channel.json"


def _generate_channel_id() -> str:
    """新規登録時のchannel_id未指定時に使う簡易な一意ID（ミリ秒epoch）。"""
    return str(int(time.time() * 1000))


def compute_channel_expiry(days: int, *, now: datetime | None = None) -> str:
    """channel_expiryのISO8601文字列を計算する。

    Zoho側で許容される最大有効期限はorgのライセンスにより異なり、本タスクでは実APIへの
    到達確認ができないため未検証（超過分はZoho側でエラーまたは切り詰めになる想定）。
    """
    base = now if now is not None else datetime.now(timezone.utc)
    return (base + timedelta(days=days)).isoformat(timespec="seconds")


def build_watch_payload(
    *,
    channel_id: str,
    module: str,
    notify_url: str,
    channel_expiry: str,
    token: str | None,
) -> dict[str, Any]:
    """`POST/PUT /crm/v3/actions/watch` のリクエストボディを組み立てる。"""
    entry: dict[str, Any] = {
        "channel_id": channel_id,
        "events": [{"channel_id": channel_id, "module": module}],
        "channel_expiry": channel_expiry,
        "notify_url": notify_url,
    }
    if token:
        # 上記docstring参照: このtokenはHTTPヘッダーではなく通知bodyのtokenフィールドとして
        # 返ってくる。受信側はverify_webhook_body_token()（_common.py）でこの値を
        # ZOHO_WEBHOOK_SECRETと照合する。
        entry["token"] = token
    return {"watch": [entry]}


def _redact_watch_token_for_display(data: dict[str, Any]) -> dict[str, Any]:
    """BLOCKER1: 標準出力への表示専用に、`watch`エントリ内のtokenフィールドの値を伏せた
    コピーを返す。実際にAPIへ送信するpayload/APIから受け取ったresultそのものは変更しない
    （呼び出し側は表示にのみこの戻り値を使うこと）。ZOHO_WEBHOOK_SECRETの実値がstdout・
    ターミナル履歴に平文で残ることを防ぐ。
    """
    redacted = copy.deepcopy(data)
    for entry in redacted.get("watch") or []:
        if isinstance(entry, dict) and "token" in entry:
            entry["token"] = "***REDACTED***"
    return redacted


def _load_persisted_channel_id(path: Path | None = None) -> str | None:
    """WARN4: 前回`--yes`実行時に保存したchannel_idを読み戻す（無ければNone）。"""
    target = path if path is not None else _CHANNEL_STATE_PATH
    try:
        data = json.loads(target.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    channel_id = data.get("channel_id")
    return str(channel_id) if channel_id else None


def _persist_channel_state(
    *, channel_id: str, channel_expiry: str, path: Path | None = None
) -> None:
    """WARN4: 登録成功時のchannel_id/channel_expiryをローカルファイルへ書き出す。
    ターミナル出力が失われても、次回実行時の延長対象（--channel-id）を復元できるようにする。
    """
    target = path if path is not None else _CHANNEL_STATE_PATH
    target.write_text(
        json.dumps({"channel_id": channel_id, "channel_expiry": channel_expiry}, ensure_ascii=False, indent=2)
        + "\n"
    )


def _build_zoho_client() -> HttpZohoClient:
    """`production_wiring.build_zoho_targets_by_db`と同じ方針で、ZOHO_ACCOUNTS_BASE_URL/
    ZOHO_API_BASE_URLが設定されていれば明示的に渡す（未設定時はHttpZohoClient既定の`.com`
    ではなくこのスクリプトの`.jp`前提の呼び出し元がwatch用URLを別途組み立てるため、
    トークンリフレッシュ用のaccounts_base_urlのみここで扱う）。
    """
    kwargs: dict[str, str] = {}
    accounts_base_url = os.environ.get("ZOHO_ACCOUNTS_BASE_URL")
    if accounts_base_url:
        kwargs["accounts_base_url"] = accounts_base_url
    api_base_url = os.environ.get("ZOHO_API_BASE_URL")
    if api_base_url:
        kwargs["api_base_url"] = api_base_url
    return HttpZohoClient(**kwargs)


def register_or_renew_watch(
    client: HttpZohoClient,
    *,
    watch_api_base_url: str,
    payload: dict[str, Any],
    is_renewal: bool,
) -> dict[str, Any]:
    """実際にwatch APIを呼び出す。新規登録はPOST、延長更新はPUT。"""
    method = "PUT" if is_renewal else "POST"
    url = f"{watch_api_base_url.rstrip('/')}/actions/watch"
    response = client.request(method, url, json_body=payload, idempotent=False)
    raise_for_error(response, ZohoApiError)
    body: dict[str, Any] = response.json()
    for entry in body.get("watch") or []:
        if entry.get("status") != "success":
            raise ZohoApiError(response.status_code, str(entry))
    return body


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--base-url",
        required=True,
        help="このデプロイのベースURL（例: https://crm-sfa-integration.vercel.app）。"
        "末尾に /api/webhooks/zoho を付けたURLをnotify_urlとして登録する。",
    )
    parser.add_argument("--module", default=_DEFAULT_MODULE, help=f"対象Zohoモジュール（既定: {_DEFAULT_MODULE}）")
    parser.add_argument(
        "--channel-id",
        default=None,
        help="既存チャンネルIDを指定すると更新（延長、PUT）として扱う。省略時は、前回`--yes`実行時に"
        f"保存された channel_id（{_CHANNEL_STATE_PATH.name}）があればそれを延長対象として使う。"
        "無ければ新規登録（POST）としてchannel_idを自動生成する。",
    )
    parser.add_argument(
        "--expiry-days", type=int, default=_DEFAULT_EXPIRY_DAYS, help=f"channel_expiryまでの日数（既定: {_DEFAULT_EXPIRY_DAYS}）"
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("ZOHO_WEBHOOK_SECRET"),
        help="watch登録時にZohoへ渡すtoken文字列（既定: 環境変数ZOHO_WEBHOOK_SECRET）。"
        "受信側（zoho_webhook.py）もZOHO_WEBHOOK_SECRETと照合するため、必ず一致させること。",
    )
    parser.add_argument(
        "--watch-api-base-url",
        default=_DEFAULT_WATCH_API_BASE_URL,
        help=f"watch APIのベースURL（既定: {_DEFAULT_WATCH_API_BASE_URL}）",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="実際にZoho APIへリクエストを送る。指定しない限りdry-run表示のみで終了する。",
    )
    parser.add_argument(
        "--allow-empty-token",
        action="store_true",
        help="BLOCKER3対策: --yes指定時にtoken（既定はZOHO_WEBHOOK_SECRET）が空でも登録を強行する。"
        "既定ではこのフラグが無い限り--yes単体で空tokenの登録は拒否する。ローカルシェルの"
        "ZOHO_WEBHOOK_SECRETがVercel本番と食い違って（あるいは未設定のまま）実行し、"
        "『登録済みだが受信側で全通知401拒否される』状態に気づかず陥る事故を防ぐため。",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    channel_id = args.channel_id
    loaded_channel_id_from_file = False
    if channel_id is None:
        channel_id = _load_persisted_channel_id()
        loaded_channel_id_from_file = channel_id is not None
    is_renewal = channel_id is not None
    if channel_id is None:
        channel_id = _generate_channel_id()
    if loaded_channel_id_from_file:
        print(
            f"（--channel-id 未指定のため、前回登録時に保存された channel_id={channel_id}"
            f"（{_CHANNEL_STATE_PATH}）を延長対象として使用します。新規に別チャンネルを登録したい"
            "場合は --channel-id で明示指定してください）"
        )
    notify_url = f"{args.base_url.rstrip('/')}/api/webhooks/zoho"
    channel_expiry = compute_channel_expiry(args.expiry_days)
    payload = build_watch_payload(
        channel_id=channel_id,
        module=args.module,
        notify_url=notify_url,
        channel_expiry=channel_expiry,
        token=args.token,
    )
    method = "PUT" if is_renewal else "POST"
    url = f"{args.watch_api_base_url.rstrip('/')}/actions/watch"

    print("=== Zoho Notifications(watch) 登録内容（dry-run表示） ===")
    print(f"  操作          : {'更新（延長）' if is_renewal else '新規登録'}")
    print(f"  method / url  : {method} {url}")
    print(f"  module        : {args.module}")
    print(f"  channel_id    : {channel_id}")
    print(f"  channel_expiry: {channel_expiry}")
    print(f"  notify_url    : {notify_url}")
    if args.token:
        print(
            "  token         : (設定あり。受信側zoho_webhook.pyがbodyのtokenフィールドを"
            "同じZOHO_WEBHOOK_SECRETと照合する)"
        )
    else:
        print(
            "  token         : (未設定。ZOHO_WEBHOOK_SECRETが受信側で設定済みだと全通知が"
            "401で拒否されるため注意)"
        )
    # BLOCKER1: 標準出力にはtokenを伏せた表示用コピーのみ出す。実際にAPIへ送るpayload自体は
    # 変更しない（--yes時は下のregister_or_renew_watch()へ生のpayloadをそのまま渡す）。
    print(json.dumps(_redact_watch_token_for_display(payload), ensure_ascii=False, indent=2))

    if not args.yes:
        print("\n--yes が指定されていないため、実際のAPI呼び出しは行いません（dry-run）。")
        return

    if not args.token and not args.allow_empty_token:
        print(
            "\nエラー: --yes が指定されましたが token が空です（ZOHO_WEBHOOK_SECRETが未設定、"
            "または --token 未指定）。この状態で登録すると、Zoho CRM側には『成功』として"
            "チャンネルが作られる一方、受信側zoho_webhook.pyの検証（body内tokenフィールド）は"
            "全通知を401で拒否し続け、気づきにくい『登録済みだが機能しない』状態に陥ります。\n"
            "ローカルシェルでVercel本番と同じ値を `export ZOHO_WEBHOOK_SECRET=...` するか、"
            "--token を明示指定してください。空tokenでの登録をどうしても行う場合のみ、"
            "--allow-empty-token を追加してください。実際のAPI呼び出しは行っていません。",
            file=sys.stderr,
        )
        raise SystemExit(1)

    print("\n--yes が指定されたため、実際にZoho APIへリクエストを送ります...")
    client = _build_zoho_client()
    result = register_or_renew_watch(
        client, watch_api_base_url=args.watch_api_base_url, payload=payload, is_renewal=is_renewal
    )
    print("完了しました:")
    # BLOCKER1: 実APIのレスポンスがtokenをエコーバックしてくる場合に備え、表示のみ同様に伏せる。
    print(json.dumps(_redact_watch_token_for_display(result), ensure_ascii=False, indent=2))

    # WARN4: channel_id/channel_expiryをローカルファイルへ保存し、ターミナル出力が失われても
    # 次回実行時の延長対象として復元できるようにする。加えてgrepしやすい1行ログも出す。
    _persist_channel_state(channel_id=channel_id, channel_expiry=channel_expiry)
    print(f"ZOHO_WATCH_CHANNEL_ID={channel_id} ZOHO_WATCH_EXPIRY={channel_expiry}")


if __name__ == "__main__":
    main()
