"""Zoho CRM Notifications（watch）チャンネルの登録・延長（renewal）に関する共有ロジック。

`scripts/register_zoho_webhook.py`（手動CLI、ローカル実行、新規登録・延長どちらも可能）と
`GET /api/cron/zoho-webhook-renewal`（`src/api/app.py`、Vercel Cron、延長専用）の
両方から使われる。payload組み立て・API呼び出し（PUT/POST `/crm/v3/actions/watch`）といった
共通ロジックをここへ集約し、重複実装を避ける。

`events`配列の形式・`channel_expiry`の1日上限など、Zoho側の制約に関する詳細は
`scripts/register_zoho_webhook.py`のモジュールdocstringを参照（本モジュールはそこで
解決済みの仕様をそのまま前提とする）。

■ channel_idの取得元について（Vercel Cronから使う場合の設計判断） -----------------------------
`.zoho_watch_channel.json`はリポジトリ直下のローカルファイルであり、`.gitignore`対象
（コミットされない）。手動CLI実行時（ローカルシェル）はこのファイルへ読み書きできるが、
Vercelのサーバーレス関数はデプロイのたびに作り直される読み取り専用に近いファイルシステム上で
動作するため、過去にローカルで`scripts/register_zoho_webhook.py --yes`を実行した際に
このファイルへ書き込まれた内容へは一切アクセスできない（`NotionIdMappingStore`が
IDマッピングの永続化にNotionページを使っているのと同種の制約）。加えて、Vercel Cronの
呼び出しはリクエストのたびに新しい実行環境が使われる可能性があり、仮に`/tmp`へ書けたと
しても次回実行時に同じファイルが残っている保証がない。

このため、`renew_zoho_watch_channel()`は`.zoho_watch_channel.json`に頼らず、
環境変数`ZOHO_WATCH_CHANNEL_ID`をchannel_idの一次情報源として使う（`channel_id`引数を
明示的に渡した場合はそちらを優先する。CLI側の`--channel-id`/ローカルファイル解決結果を
そのまま渡せるようにするため）。

Zoho公式ドキュメント（Notifications API v3）には、既知のchannel_idを指定せずに
登録済みチャンネルの一覧を取得できるGETエンドポイントの記載が見当たらず
（`GET /crm/v3/actions/watch`は特定channel_idの詳細取得用と見られる）、本タスクでは
実際のZoho本番APIへ新たに到達して仕様を確認すること自体がスコープ外だったため、
「一覧取得して既存チャンネルを探す」というアプローチを未検証のまま採用することは避けた。
環境変数方式は追加のAPI呼び出しも不要で、`scripts/register_zoho_webhook.py`が既に
`ZOHO_WATCH_CHANNEL_ID=... ZOHO_WATCH_EXPIRY=...`という1行をgrepしやすい形で出力する
運用と自然に噛み合う（運用者がその値をコピーしてVercel環境変数へ設定するだけでよい）。

運用上は、`scripts/register_zoho_webhook.py --yes`で新規登録するたびに出力される
channel_idを、Vercel本番環境変数`ZOHO_WATCH_CHANNEL_ID`へも手動で反映すること
（`vercel env add ZOHO_WATCH_CHANNEL_ID`）。新規登録時のみこの反映が必要で、
それ以降のcronによる延長ではchannel_idは変わらない（PUTは同じchannel_idを指定し
続けるだけで、Zoho側もchannel_idを変更しない）。

`channel_id`引数も環境変数`ZOHO_WATCH_CHANNEL_ID`もどちらも得られない場合、
`renew_zoho_watch_channel()`は`ZohoWatchChannelNotConfiguredError`を送出する（Zoho APIへは
到達しない）。黙って何もせず200を返す「成功したように見えるno-op」は、通知が無音で
止まっていることに誰も気づけない最悪のケースのため、明確なエラーとして表面化させる。
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from src.sync_engine.clients._http import raise_for_error
from src.sync_engine.clients.zoho_client import HttpZohoClient, ZohoApiError

DEFAULT_MODULE = "Deals"
# Zoho公式ドキュメント記載の制約: channel_expiryは登録・延長時点から最大1日先まで。
MAX_EXPIRY_DAYS = 1
DEFAULT_EXPIRY_DAYS = 1
# 本番Zoho orgは.jpデータセンター所属（ZOHO_ACCOUNTS_BASE_URL/ZOHO_API_BASE_URLと同じ理由）。
# HttpZohoClientのCRUD用api_base_urlは`/crm/v2`固定だが、watch(Notifications) APIは`/crm/v3`。
DEFAULT_WATCH_API_BASE_URL = "https://www.zohoapis.jp/crm/v3"
# Vercel Cronからの延長時、channel_idの一次情報源として読む環境変数（モジュールdocstring参照）。
WATCH_CHANNEL_ID_ENV_VAR = "ZOHO_WATCH_CHANNEL_ID"
# Vercel Cronからの延長時、notify_url組み立てに使うデプロイのベースURL
# （例: https://crm-sfa-integration.vercel.app）。scripts/register_zoho_webhook.pyの
# --base-url引数と同じ役割。
WEBHOOK_BASE_URL_ENV_VAR = "ZOHO_WEBHOOK_BASE_URL"


class ZohoWatchChannelNotConfiguredError(Exception):
    """channel_id（延長対象）またはnotify_url組み立てに必要なbase URLが判明せず、
    watchチャンネルの延長処理を実行できない場合に送出する。"""


def generate_channel_id() -> str:
    """channel_id未指定時に使う簡易な一意ID（ミリ秒epoch）。"""
    return str(int(time.time() * 1000))


def compute_channel_expiry(days: int, *, now: datetime | None = None) -> str:
    """channel_expiryのISO8601文字列を計算する。

    Zoho公式ドキュメントによれば、channel_expiryは登録・延長時点から最大1日先までしか
    許容されない。呼び出し側は`MAX_EXPIRY_DAYS`超過を事前に`validate_expiry_days()`で
    拒否してからこの関数を呼ぶこと（この関数自体はクランプや検証を行わない）。
    """
    base = now if now is not None else datetime.now(timezone.utc)
    return (base + timedelta(days=days)).isoformat(timespec="seconds")


def validate_expiry_days(days: int) -> None:
    """`expiry_days`がZoho側の上限（`MAX_EXPIRY_DAYS`）を超えていないか検証する。

    超過している場合、本番Zoho APIへ送って`INVALID_DATA`で拒否される事故を繰り返さないよう、
    実際にAPIへ到達する前に明確なエラーメッセージ付きで拒否する。
    """
    if days > MAX_EXPIRY_DAYS:
        raise ValueError(
            f"expiry_days に {days} が指定されましたが、Zoho側の制約により"
            f"channel_expiryは登録・延長時点から最大{MAX_EXPIRY_DAYS}日先までしか許容されません"
            "（超過した値を送るとINVALID_DATAで拒否されます）。"
            f"{MAX_EXPIRY_DAYS}以下の値を指定してください。"
        )


def build_watch_payload(
    *,
    channel_id: str,
    module: str,
    notify_url: str,
    channel_expiry: str,
    token: str | None,
) -> dict[str, Any]:
    """`POST/PUT /crm/v3/actions/watch` のリクエストボディを組み立てる。

    `events`はZoho公式ドキュメント記載のスキーマ通り、`"{モジュールAPI名}.{操作}"`形式の
    文字列を並べたフラットな配列で送る（オブジェクト配列ではない）。対象モジュールの
    全操作を監視したいため`"{module}.all"`の1件のみを含める。
    """
    entry: dict[str, Any] = {
        "channel_id": channel_id,
        "events": [f"{module}.all"],
        "channel_expiry": channel_expiry,
        "notify_url": notify_url,
    }
    if token:
        # このtokenはHTTPヘッダーではなく通知body内のtokenフィールドとして返ってくる。
        # 受信側はverify_webhook_body_token()（_common.py）でこの値をZOHO_WEBHOOK_SECRETと
        # 照合する（詳細は scripts/register_zoho_webhook.py のモジュールdocstring参照）。
        entry["token"] = token
    return {"watch": [entry]}


def redact_watch_entry_token(entry: Any) -> Any:
    """`watch`配列内の1エントリについて、`token`フィールドの値を伏せたコピーを返す。

    エントリがdictでない、または`token`フィールドを持たない場合はそのまま返す。
    Zoho watch APIは登録時に送った`token`（実体は`ZOHO_WEBHOOK_SECRET`）をレスポンスへ
    そのままエコーバックしてくることがあるため、Zohoの応答エントリを文字列化・ログ出力・
    表示する前には必ずこれを通すこと（例外メッセージ・CLI表示の両方から共有して使う）。
    """
    if not isinstance(entry, dict) or "token" not in entry:
        return entry
    redacted = dict(entry)
    redacted["token"] = "***REDACTED***"
    return redacted


def register_or_renew_watch(
    client: HttpZohoClient,
    *,
    watch_api_base_url: str,
    payload: dict[str, Any],
    is_renewal: bool,
) -> dict[str, Any]:
    """実際にwatch APIを呼び出す。新規登録はPOST、延長更新はPUT。

    Zohoが2xxを返した場合でも、以下のいずれかに該当する場合は「見た目だけ成功したように
    見えるno-op」を防ぐためZohoApiErrorを送出する（本モジュールdocstring・
    `renew_zoho_watch_channel()`のdocstring参照）。
    - レスポンスbodyがJSONとして解釈できない、またはJSONオブジェクト（dict）でない
    - `watch`配列が欠落している、または空
    - `watch`配列内のエントリがdictでない
    - `watch`配列内のいずれかのエントリの`status`が`"success"`でない
    - 送信したpayloadのchannel_idと一致する`status: "success"`エントリが1件も無い
      （channel_idが送信payloadから判別できる場合のみこの照合を行う）

    エラーメッセージへ含めるZoho応答エントリは、`token`フィールドをエコーバックしてくる
    可能性があるため、必ず`redact_watch_entry_token()`を通してから文字列化する。
    """
    method = "PUT" if is_renewal else "POST"
    url = f"{watch_api_base_url.rstrip('/')}/actions/watch"
    response = client.request(method, url, json_body=payload, idempotent=False)
    raise_for_error(response, ZohoApiError)

    try:
        body = response.json()
    except ValueError as exc:
        raise ZohoApiError(
            response.status_code, "zoho watch api response body was not valid JSON"
        ) from exc
    if not isinstance(body, dict):
        raise ZohoApiError(
            response.status_code,
            "zoho watch api response body was not a JSON object (unexpected shape)",
        )

    requested_channel_id: Any = None
    requested_watch = payload.get("watch")
    if isinstance(requested_watch, list) and requested_watch and isinstance(requested_watch[0], dict):
        requested_channel_id = requested_watch[0].get("channel_id")

    watch_entries = body.get("watch")
    if not watch_entries:
        raise ZohoApiError(
            response.status_code,
            "zoho watch api response did not include any confirming watch entries",
        )

    confirmed = False
    for entry in watch_entries:
        if not isinstance(entry, dict):
            raise ZohoApiError(
                response.status_code,
                f"zoho watch api returned an unexpected watch entry shape: {type(entry).__name__}",
            )
        if entry.get("status") != "success":
            raise ZohoApiError(response.status_code, str(redact_watch_entry_token(entry)))
        if requested_channel_id is None or entry.get("channel_id") == requested_channel_id:
            confirmed = True

    if not confirmed:
        raise ZohoApiError(
            response.status_code,
            "zoho watch api response did not confirm the requested channel_id",
        )

    return body


def build_zoho_client_from_env() -> HttpZohoClient:
    """`production_wiring.build_zoho_targets_by_db`と同じ方針で、ZOHO_ACCOUNTS_BASE_URL/
    ZOHO_API_BASE_URLが設定されていれば明示的に渡す（未設定時はHttpZohoClient既定の`.com`）。
    トークンリフレッシュ用のaccounts_base_urlのみここで扱い、watch用URL（`.jp`前提）は
    呼び出し元が別途`watch_api_base_url`として組み立てる。
    """
    kwargs: dict[str, str] = {}
    accounts_base_url = os.environ.get("ZOHO_ACCOUNTS_BASE_URL")
    if accounts_base_url:
        kwargs["accounts_base_url"] = accounts_base_url
    api_base_url = os.environ.get("ZOHO_API_BASE_URL")
    if api_base_url:
        kwargs["api_base_url"] = api_base_url
    return HttpZohoClient(**kwargs)


def renew_zoho_watch_channel(
    client: HttpZohoClient,
    *,
    channel_id: str | None = None,
    module: str = DEFAULT_MODULE,
    notify_url: str | None = None,
    token: str | None = None,
    expiry_days: int = DEFAULT_EXPIRY_DAYS,
    watch_api_base_url: str = DEFAULT_WATCH_API_BASE_URL,
) -> dict[str, Any]:
    """既存のZoho watchチャンネルをPUTで延長する（`GET /api/cron/zoho-webhook-renewal`から
    呼ばれることを主眼に設計。CLI側も`channel_id`/`notify_url`を明示的に渡せば利用できる）。

    - `channel_id`省略時は環境変数`ZOHO_WATCH_CHANNEL_ID`を読む。
    - `notify_url`省略時は環境変数`ZOHO_WEBHOOK_BASE_URL`から組み立てる
      （`{base_url}/api/webhooks/zoho`）。
    - どちらの解決にも失敗した場合は`ZohoWatchChannelNotConfiguredError`を送出する
      （Zoho APIへは到達しない。モジュールdocstring参照）。
    """
    resolved_channel_id = channel_id if channel_id is not None else os.environ.get(WATCH_CHANNEL_ID_ENV_VAR)
    if not resolved_channel_id:
        raise ZohoWatchChannelNotConfiguredError(
            f"channel_idが指定されておらず、環境変数{WATCH_CHANNEL_ID_ENV_VAR}も未設定のため、"
            "延長対象のZoho watchチャンネルを特定できません。scripts/register_zoho_webhook.py "
            "--yes で新規登録した際に出力される channel_id を、Vercel本番環境変数 "
            f"{WATCH_CHANNEL_ID_ENV_VAR} に設定してください（vercel env add {WATCH_CHANNEL_ID_ENV_VAR}）。"
        )

    resolved_notify_url = notify_url
    if not resolved_notify_url:
        base_url = os.environ.get(WEBHOOK_BASE_URL_ENV_VAR)
        if not base_url:
            raise ZohoWatchChannelNotConfiguredError(
                f"notify_urlが指定されておらず、環境変数{WEBHOOK_BASE_URL_ENV_VAR}も未設定のため、"
                "延長リクエストのnotify_urlを組み立てられません。デプロイのベースURL"
                "（例: https://crm-sfa-integration.vercel.app）を"
                f"{WEBHOOK_BASE_URL_ENV_VAR}へ設定してください。"
            )
        resolved_notify_url = f"{base_url.rstrip('/')}/api/webhooks/zoho"

    validate_expiry_days(expiry_days)
    channel_expiry = compute_channel_expiry(expiry_days)
    payload = build_watch_payload(
        channel_id=resolved_channel_id,
        module=module,
        notify_url=resolved_notify_url,
        channel_expiry=channel_expiry,
        token=token,
    )
    response = register_or_renew_watch(
        client, watch_api_base_url=watch_api_base_url, payload=payload, is_renewal=True
    )
    return {
        "channel_id": resolved_channel_id,
        "module": module,
        "channel_expiry": channel_expiry,
        "notify_url": resolved_notify_url,
        "response": response,
    }
