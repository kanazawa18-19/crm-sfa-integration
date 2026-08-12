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
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from src.sync_engine.clients._http import raise_for_error
from src.sync_engine.clients.zoho_client import HttpZohoClient, ZohoApiError

# 2026-08-12、`ZOHO_LABEL_FIELD_MAPPINGS`（zoho_field_transforms.py）でフィールドマッピングの
# カバレッジを6モジュール分揃えたことに合わせ、watchチャンネルの購読対象もこの6モジュール
# 全てへ拡張する（Zoho Notifications APIは1つのwatchエントリの`events`配列に複数モジュールの
# 操作を混在させられるため、モジュールごとに別チャンネルを作る必要はない）。
# `renew_zoho_watch_channel()`（cron自動延長）の既定値として使う。
DEFAULT_MODULES: list[str] = [
    "Deals",  # project
    "CustomModule3",  # chain
    "CustomModule2",  # action
    "Accounts",  # client_master
    "Contacts",  # contact
    "Products",  # product
]
# Zoho公式ドキュメント記載の制約: channel_expiryは登録・延長時点から最大1日先まで。
MAX_EXPIRY_DAYS = 1
# scripts/register_zoho_webhook.py（手動CLI）の既定値。新規登録・手動延長では上限いっぱいの
# 1日を要求してよい（次にいつ人間が延長するか分からないため、猶予は長いほど安全）。
DEFAULT_EXPIRY_DAYS = 1
# `renew_zoho_watch_channel()`（Vercel Cronからの自動延長専用）の既定値。VercelがHobbyプランの
# ため`GET /api/cron/zoho-webhook-renewal`は1日1回しか実行できない（vercel.jsonの
# `zoho-webhook-renewal`スケジュール参照）。もしここでMAX_EXPIRY_DAYS（24h）いっぱいを
# 要求すると、cronの実行間隔（約24h）とchannel_expiryの上限（24h）がほぼ一致してしまい、
# 1回のcron実行が少しでも遅延・失敗すると安全マージンがゼロのままチャンネルが失効し、
# `/api/webhooks/zoho`への通知が無音で止まる。そのため自動延長では上限より短い21時間
# （24h上限に対し3時間の安全マージン）を要求する（詳細は
# docs/zoho_webhook_activation_note.md参照）。
CRON_RENEWAL_EXPIRY_HOURS = 21
CRON_RENEWAL_EXPIRY_DAYS = CRON_RENEWAL_EXPIRY_HOURS / 24
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


def compute_channel_expiry(days: int | float, *, now: datetime | None = None) -> str:
    """channel_expiryのISO8601文字列を計算する。

    Zoho公式ドキュメントによれば、channel_expiryは登録・延長時点から最大1日先までしか
    許容されない。呼び出し側は`MAX_EXPIRY_DAYS`超過を事前に`validate_expiry_days()`で
    拒否してからこの関数を呼ぶこと（この関数自体はクランプや検証を行わない）。

    `days`は整数（CLIの`--expiry-days`）に加え、`CRON_RENEWAL_EXPIRY_DAYS`のような
    端数日（時間単位の安全マージンを表現するため）も受け付ける。
    """
    base = now if now is not None else datetime.now(timezone.utc)
    return (base + timedelta(days=days)).isoformat(timespec="seconds")


def validate_expiry_days(days: int | float) -> None:
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
    modules: Sequence[str],
    notify_url: str,
    channel_expiry: str,
    token: str | None,
) -> dict[str, Any]:
    """`POST/PUT /crm/v3/actions/watch` のリクエストボディを組み立てる。

    `events`はZoho公式ドキュメント記載のスキーマ通り、`"{モジュールAPI名}.{操作}"`形式の
    文字列を並べたフラットな配列で送る（オブジェクト配列ではない）。対象モジュールの
    全操作を監視したいため各モジュールにつき`"{module}.all"`を1件ずつ含める。

    Zoho公式ドキュメントの例（`"events": ["Solutions.create", "Price_Books.create",
    "Contacts.create", "Solutions.edit"]`）が示す通り、1つのwatchエントリの`events`配列は
    複数モジュールの操作を混在させられる。そのため`modules`に複数モジュールを渡した場合も
    watchエントリ（`channel_id`/`notify_url`/`token`）は1件のまま、`events`配列だけが
    モジュール数分伸びる（モジュールごとに別チャンネルを作る必要はない）。
    """
    entry: dict[str, Any] = {
        "channel_id": channel_id,
        "events": [f"{m}.all" for m in modules],
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


def _confirmed_channel_ids(entry: Any) -> set[str]:
    """`watch`応答の1エントリから、実際に確認されたchannel_idの集合を取り出す。

    2026-08-12、本番Zoho API（.jpデータセンター）への実登録・実延長で確認した実際の
    レスポンス形状: 成功エントリのchannel_idはエントリ直下（`entry["channel_id"]`）には
    存在せず、`entry["details"]["events"][*]["channel_id"]`にネストされている
    （Zoho公式ドキュメントの例には無い実挙動。事前に別のchannel_id直下チェックのみで
    実装し、本番延長が常に`did not confirm the requested channel_id`で失敗する不具合と
    なったため、実際のAPIレスポンスを直接確認した上で修正した）。
    形状が想定と異なる場合（dictでない・キー欠落等）はクラッシュせず空集合を返す。

    このchannel_id照合だけでは「要求した6モジュールのうち一部だけがレスポンスへ
    含まれていても全体を成功扱いにしてしまう」抜け穴があるため（1つのwatchエントリの
    `events`配列に複数モジュールをまとめている以上、channel_id自体は各要素で共通であり
    channel_idの一致だけではモジュール単位の欠落を検出できない）、モジュール単位の
    照合は別途`_confirmed_modules()`で行う（`register_or_renew_watch()`参照）。
    """
    if not isinstance(entry, dict):
        return set()
    details = entry.get("details")
    if not isinstance(details, dict):
        return set()
    events = details.get("events")
    if not isinstance(events, list):
        return set()
    return {
        event["channel_id"]
        for event in events
        if isinstance(event, dict) and isinstance(event.get("channel_id"), str)
    }


# `_confirmed_modules()`が`details.events[*]`の各要素からモジュール名を読み取る際に試す
# フィールド名の候補（優先順）。"module"は本モジュールの既存テスト
# （test_confirms_channel_id_from_response_with_one_event_entry_per_module）が実際の
# レスポンス形として想定してきたフィールド。"resource_name"/"api_name"は
# scripts/register_zoho_webhook.pyのモジュールdocstringに記載の、Zoho公式ドキュメントの
# watch詳細GETレスポンス例に現れるフィールド名（ただし同ドキュメントの記載はGET専用
# エンドポイント向けであり、POST/PUT `/actions/watch`のレスポンスに同じフィールドが
# 含まれるかどうかは、本番Zoho APIへ6モジュール分をまとめて登録・延長した実際の
# レスポンスではまだ確認できていない。`_confirmed_channel_ids()`のchannel_idネストと
# 違い、複数モジュール一括登録での実地検証はまだ済んでいない）。次回、実際に6モジュール
# 分をまとめて本番登録・延長した際に実レスポンスのevents要素を確認し、想定と異なって
# いればこの候補リストとdocstringを更新すること。
_MODULE_FIELD_CANDIDATES: tuple[str, ...] = ("module", "resource_name", "api_name")


def _confirmed_modules(entry: Any) -> set[str]:
    """`watch`応答の1エントリから、`details.events[*]`の各要素に含まれるモジュール名
    （`_MODULE_FIELD_CANDIDATES`のいずれかのフィールド）を読み取り、集合として返す。

    レスポンスにモジュール識別用フィールドが一切含まれない場合（フィールド名が
    未確認のため今のところ実際に起こりうる。上記`_MODULE_FIELD_CANDIDATES`の
    コメント参照）は空集合を返す。`register_or_renew_watch()`はこの関数が空集合を
    返した場合、モジュール単位の照合を行わない（後方互換のため。channel_id照合の
    みで確認済みとする）。形状が想定と異なる場合（dictでない・キー欠落等）は
    クラッシュせず空集合として扱う。
    """
    if not isinstance(entry, dict):
        return set()
    details = entry.get("details")
    if not isinstance(details, dict):
        return set()
    events = details.get("events")
    if not isinstance(events, list):
        return set()
    modules: set[str] = set()
    for event in events:
        if not isinstance(event, dict):
            continue
        for field in _MODULE_FIELD_CANDIDATES:
            value = event.get(field)
            if isinstance(value, str) and value:
                modules.add(value)
                break
    return modules


def _requested_modules_from_payload(payload: dict[str, Any]) -> set[str]:
    """送信したpayloadの`watch[0]["events"]`（`"{module}.{action}"`形式の文字列配列。
    `build_watch_payload()`参照）から、要求したモジュール名の集合を復元する。

    モジュールAPI名自体にはドット（`.`）は含まれない（`DEFAULT_MODULES`参照）ため、
    先頭のドットまでをモジュール名として切り出せば十分。
    """
    requested_watch = payload.get("watch")
    if not (isinstance(requested_watch, list) and requested_watch and isinstance(requested_watch[0], dict)):
        return set()
    events_sent = requested_watch[0].get("events")
    if not isinstance(events_sent, list):
        return set()
    return {
        event_str.split(".", 1)[0]
        for event_str in events_sent
        if isinstance(event_str, str) and "." in event_str
    }


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
    - 【WARN1対策】要求した`modules`（1つのwatchエントリの`events`配列にまとめて含めた
      複数モジュール）のうち、レスポンスから確認できたモジュール（`_confirmed_modules()`
      参照）に含まれていないものが1件でもある場合。channel_idの一致だけでは「6モジュール
      要求したのに実際には5モジュール分しか登録されなかった」というような部分的な失敗を
      検出できない（channel_id自体は`events`配列内の全要素で共通のため）。この照合は
      レスポンスからモジュール名を1件も読み取れなかった場合（フィールド名が未確認・
      レスポンス形状が想定外等）はスキップする（`_confirmed_modules()`のdocstring参照。
      後方互換のため、channel_id照合のみで確認済みとする）。

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
    requested_modules = _requested_modules_from_payload(payload)

    watch_entries = body.get("watch")
    if not watch_entries:
        raise ZohoApiError(
            response.status_code,
            "zoho watch api response did not include any confirming watch entries",
        )

    confirmed = False
    confirmed_modules: set[str] = set()
    for entry in watch_entries:
        if not isinstance(entry, dict):
            raise ZohoApiError(
                response.status_code,
                f"zoho watch api returned an unexpected watch entry shape: {type(entry).__name__}",
            )
        if entry.get("status") != "success":
            raise ZohoApiError(response.status_code, str(redact_watch_entry_token(entry)))
        if requested_channel_id is None or requested_channel_id in _confirmed_channel_ids(entry):
            confirmed = True
            confirmed_modules |= _confirmed_modules(entry)

    if not confirmed:
        raise ZohoApiError(
            response.status_code,
            "zoho watch api response did not confirm the requested channel_id",
        )

    # 【WARN1対策】レスポンスからモジュール名を1件も読み取れた場合のみ、要求した全モジュールが
    # 揃っているかを照合する（読み取れなかった場合はスキップ。docstring・_confirmed_modules()参照）。
    if confirmed_modules:
        missing_modules = requested_modules - confirmed_modules
        if missing_modules:
            raise ZohoApiError(
                response.status_code,
                "zoho watch api response confirmed the channel_id but did not confirm all "
                f"requested modules; missing module(s): {', '.join(sorted(missing_modules))} "
                "(live sync for this/these module(s) would silently stop working). "
                f"confirmed module(s): {', '.join(sorted(confirmed_modules))}. "
                f"response watch entries: {[redact_watch_entry_token(e) for e in watch_entries]}",
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
    modules: Sequence[str] = DEFAULT_MODULES,
    notify_url: str | None = None,
    token: str | None = None,
    expiry_days: int | float = CRON_RENEWAL_EXPIRY_DAYS,
    watch_api_base_url: str = DEFAULT_WATCH_API_BASE_URL,
) -> dict[str, Any]:
    """既存のZoho watchチャンネルをPUTで延長する（`GET /api/cron/zoho-webhook-renewal`から
    呼ばれることを主眼に設計。CLI側も`channel_id`/`notify_url`を明示的に渡せば利用できる）。

    - `channel_id`省略時は環境変数`ZOHO_WATCH_CHANNEL_ID`を読む。
    - `notify_url`省略時は環境変数`ZOHO_WEBHOOK_BASE_URL`から組み立てる
      （`{base_url}/api/webhooks/zoho`）。
    - どちらの解決にも失敗した場合は`ZohoWatchChannelNotConfiguredError`を送出する
      （Zoho APIへは到達しない。モジュールdocstring参照）。
    - `expiry_days`の既定値は`DEFAULT_EXPIRY_DAYS`（1日=Zoho上限いっぱい）ではなく
      `CRON_RENEWAL_EXPIRY_DAYS`（21時間）。Vercel Hobbyプランの制約により本関数の主要な
      呼び出し元であるcronは1日1回しか実行されないため、上限いっぱいを要求すると
      cronの実行間隔と失効タイミングがほぼ一致し安全マージンがゼロになる
      （`CRON_RENEWAL_EXPIRY_HOURS`のコメント・docs/zoho_webhook_activation_note.md参照）。
    - `modules`の既定値は`DEFAULT_MODULES`（フィールドマッピングでカバー済みの6モジュール
      全て）。以前は`module: str`単数引数（既定`Deals`のみ）だったが、これを延長し忘れると
      `Deals`以外のモジュール変更がwebhook経由でNotionへ反映されず気づきにくいため、
      モジュール単位の環境変数オーバーライドは設けず、常に固定の6モジュール分をまとめて
      延長する方針にした（従来`module`用の環境変数オーバーライドは存在しなかったため、
      後方互換の考慮も不要）。個別モジュールのみ延長したいテスト・呼び出しは`modules`引数へ
      明示的にリストを渡せばよい。
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
        modules=modules,
        notify_url=resolved_notify_url,
        channel_expiry=channel_expiry,
        token=token,
    )
    response = register_or_renew_watch(
        client, watch_api_base_url=watch_api_base_url, payload=payload, is_renewal=True
    )
    return {
        "channel_id": resolved_channel_id,
        "modules": list(modules),
        "channel_expiry": channel_expiry,
        "notify_url": resolved_notify_url,
        "response": response,
    }
