#!/usr/bin/env python3
"""Zoho CRM Notifications（watch）APIへ、本デプロイのZoho Webhook受信エンドポイント
（`POST /api/webhooks/zoho`, `src/sync_engine/webhook_handlers/zoho_webhook.py`）を
購読登録／更新（延長）するスクリプト。

既定では `Deals`（project）/`CustomModule3`（chain）/`CustomModule2`（action）/
`Accounts`（client_master）/`Contacts`（contact）/`Products`（product）の6モジュール全てを
1つのwatchチャンネルで購読登録する（`src/sync_engine/zoho_watch_channel.py`の
`DEFAULT_MODULES`。各`DatabaseSchema.zoho_api_module`と一致させたもの）。Zoho Notifications
APIは1つのwatchエントリの`events`配列に複数モジュールの操作を混在させられるため、
モジュールごとに別チャンネルを作る必要はない。`--module`を明示指定（複数回指定可）すると
対象モジュールを絞り込める。認証は `src/sync_engine/clients/zoho_client.py` の
`HttpZohoClient` を再利用し、`ZOHO_CLIENT_ID`/`ZOHO_CLIENT_SECRET`/`ZOHO_REFRESH_TOKEN` からの
トークンリフレッシュ・キャッシュをそのまま流用する（本スクリプト独自の認証パスは持たない）。

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

■ events配列の形式・channel_expiryの上限（解決済み） -------------------------------------------
初回の実装では`events`を`[{"channel_id": ..., "module": ...}]`という形のオブジェクト配列と
誤って組み立てており、実際に本番Zoho（dry-run+`--yes`）へ送ったところ
`HTTP 202: {'code': 'INVALID_DATA', 'details': {'api_name': 'events', 'json_path': '$.watch[0].events'}}`
で拒否された。Zoho公式ドキュメント記載のリクエストスキーマを確認した結果、正しい形は
以下の通り。

    {
      "watch": [
        {
          "channel_id": "1000000068001",
          "events": ["Deals.all"],
          "channel_expiry": "2018-02-02T10:30:00+05:30",
          "token": "...",
          "notify_url": "https://..."
        }
      ]
    }

- `events`は`"{モジュールAPI名}.{create|delete|edit|all}"`形式の文字列を並べたフラットな配列。
  本スクリプトは対象モジュール全体の変更を監視したいため`["{module}.all" for module in modules]`を
  送る（`modules`は`--module`（複数回指定可）/既定`DEFAULT_MODULES`の6モジュール）。Zoho公式
  ドキュメントの例（`"events": ["Solutions.create", "Price_Books.create", "Contacts.create",
  "Solutions.edit"]`）通り、1つのwatchエントリの`events`配列に複数モジュールの操作を
  混在させられるため、モジュール数が増えてもwatchエントリ自体は1件のままでよい。
- `channel_expiry`は登録・延長時点から**最大1日先まで**（Zoho側の制約）。それを超える値を
  指定すると、以前と同様のINVALID_DATAで本番Zoho APIへ拒否される。本スクリプトは
  `--expiry-days`が`_MAX_EXPIRY_DAYS`（1日）を超える場合、実際にAPIへ送る前に明確な
  エラーメッセージ付きで拒否する（dry-run表示前に検証する）。
- `resource_uri`/`resource_name`/`module`といったフィールドは、Zohoのwatch詳細GETレスポンス
  側にのみ現れるものであり、登録リクエストのペイロードには含めない。

■ このスクリプトが行うこと ---------------------------------------------------------------------
1. 対象モジュール（既定は`DEFAULT_MODULES`の6モジュール、`--module`で絞り込み可）向けの
   watchペイロード（channel_id/events/channel_expiry/notify_url/token）を組み立てて表示する
   （常に実行される。dry-run表示）。
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

■ 有効期限切れ前の自動延長について
Zohoのwatchチャンネルは最大1日で失効するため、本スクリプトの手動実行だけに頼らず、
`GET /api/cron/zoho-webhook-renewal`（`src/api/app.py`、Vercel Cronで6時間毎に自動起動）が
定期的に延長（PUT）する。自動延長のロジックは`src/sync_engine/zoho_watch_channel.py`の
`renew_zoho_watch_channel()`に切り出してあり、本スクリプトの`build_watch_payload`/
`register_or_renew_watch`等と共通化している。ただし自動延長はVercel環境変数
`ZOHO_WATCH_CHANNEL_ID`をchannel_idの一次情報源とする（本スクリプトが使う
`.zoho_watch_channel.json`はVercelのサーバーレス関数からは参照できないローカルファイルの
ため）。新規登録（本スクリプトを`--channel-id`無しで初回`--yes`実行した場合）の直後は、
出力される`ZOHO_WATCH_CHANNEL_ID=...`をVercel本番環境変数`ZOHO_WATCH_CHANNEL_ID`へも
手動で反映すること（詳細は`docs/zoho_webhook_activation_note.md`参照）。

使い方:
    # dry-run（常定。何が送られるかを確認するだけ。--module省略時はDEFAULT_MODULESの6モジュール）
    python scripts/register_zoho_webhook.py --base-url https://crm-sfa-integration.vercel.app

    # 既存チャンネルの延長（channel_idは前回登録時のレスポンス/ログから取得したもの）
    python scripts/register_zoho_webhook.py --base-url https://crm-sfa-integration.vercel.app \\
        --channel-id 1000000026001

    # 対象モジュールを絞り込みたい場合は--moduleを繰り返し指定する
    python scripts/register_zoho_webhook.py --base-url https://crm-sfa-integration.vercel.app \\
        --module Deals --module Contacts

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
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.sync_engine.zoho_watch_channel import (
    DEFAULT_EXPIRY_DAYS as _DEFAULT_EXPIRY_DAYS,
    DEFAULT_MODULES as _DEFAULT_MODULES,
    DEFAULT_WATCH_API_BASE_URL as _DEFAULT_WATCH_API_BASE_URL,
    MAX_EXPIRY_DAYS as _MAX_EXPIRY_DAYS,
    build_watch_payload,
    build_zoho_client_from_env as _build_zoho_client,
    compute_channel_expiry,
    generate_channel_id as _generate_channel_id,
    redact_watch_entry_token,
    register_or_renew_watch,
    validate_expiry_days,
)

# WARN4: 登録成功時のchannel_id/channel_expiryを控えておくローカルファイル（リポジトリ直下）。
# 次回実行時に--channel-id省略時のデフォルト（延長対象）として読み戻す。PIIは含まないが
# 運用メタ情報のためリポジトリにはコミットしない（.gitignore参照）。
#
# 本ファイルはローカルシェルでの手動CLI実行専用の永続化先である点に注意。Vercel Cronから
# 呼ばれる自動延長（`GET /api/cron/zoho-webhook-renewal`、`src/sync_engine/zoho_watch_channel.py`
# の`renew_zoho_watch_channel()`）はこのファイルを一切参照しない
# （Vercelのサーバーレス関数はこのファイルへアクセスできないため。詳細は
# `zoho_watch_channel.py`のモジュールdocstring参照）。
_CHANNEL_STATE_PATH = Path(__file__).resolve().parent.parent / ".zoho_watch_channel.json"


def _redact_watch_token_for_display(data: dict[str, Any]) -> dict[str, Any]:
    """BLOCKER1: 標準出力への表示専用に、`watch`エントリ内のtokenフィールドの値を伏せた
    コピーを返す。実際にAPIへ送信するpayload/APIから受け取ったresultそのものは変更しない
    （呼び出し側は表示にのみこの戻り値を使うこと）。ZOHO_WEBHOOK_SECRETの実値がstdout・
    ターミナル履歴に平文で残ることを防ぐ。エントリ単位の実際の伏せ処理は
    `zoho_watch_channel.redact_watch_entry_token()`（`register_or_renew_watch()`の
    エラーメッセージ生成でも使う共有ロジック）を再利用する。
    """
    redacted = copy.deepcopy(data)
    redacted["watch"] = [redact_watch_entry_token(entry) for entry in redacted.get("watch") or []]
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
    parser.add_argument(
        "--module",
        dest="modules",
        action="append",
        default=None,
        help="対象Zohoモジュール。複数指定する場合は--moduleを繰り返す"
        f"（例: --module Deals --module Contacts）。省略時は既定の{len(_DEFAULT_MODULES)}モジュール"
        f"全て（{', '.join(_DEFAULT_MODULES)}）を対象とする。",
    )
    parser.add_argument(
        "--channel-id",
        default=None,
        help="既存チャンネルIDを指定すると更新（延長、PUT）として扱う。省略時は、前回`--yes`実行時に"
        f"保存された channel_id（{_CHANNEL_STATE_PATH.name}）があればそれを延長対象として使う。"
        "無ければ新規登録（POST）としてchannel_idを自動生成する。",
    )
    parser.add_argument(
        "--expiry-days",
        type=int,
        default=_DEFAULT_EXPIRY_DAYS,
        help=f"channel_expiryまでの日数（既定: {_DEFAULT_EXPIRY_DAYS}）。Zoho側の制約により"
        f"{_MAX_EXPIRY_DAYS}を超える値は登録前にエラーで拒否される。",
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
    args = parser.parse_args(argv)
    if args.modules is None:
        args.modules = list(_DEFAULT_MODULES)
    return args


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    try:
        validate_expiry_days(args.expiry_days)
    except ValueError as exc:
        print(f"\nエラー: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

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
        modules=args.modules,
        notify_url=notify_url,
        channel_expiry=channel_expiry,
        token=args.token,
    )
    method = "PUT" if is_renewal else "POST"
    url = f"{args.watch_api_base_url.rstrip('/')}/actions/watch"

    print("=== Zoho Notifications(watch) 登録内容（dry-run表示） ===")
    print(f"  操作          : {'更新（延長）' if is_renewal else '新規登録'}")
    print(f"  method / url  : {method} {url}")
    print(f"  modules       : {', '.join(args.modules)}")
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
