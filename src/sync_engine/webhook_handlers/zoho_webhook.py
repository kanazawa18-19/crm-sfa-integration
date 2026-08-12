"""Zoho CRM Webhookの受信ハンドラ（05_同期・競合制御「変更検知の仕組み」: Notification Webhook）。

01_システム構成「疎結合設計」：ENABLE_ZOHOがFalseの場合、本ハンドラはペイロードの変換すら
行わず早期リターンする（他システムに一切影響を与えずZoho連携を切り離せることを保証する）。

実際のペイロード例（2026-08-12、本番Zoho CRM Notifications APIの実際の通知で確認した形状。
テストフィクスチャは tests/sync_engine/webhook_handlers/ を参照）。module は
DatabaseSchema.zoho_api_module（実際のZoho CRM APIモジュール名。例: Deals）と対応させる。
zoho_key（移行元モジュール名の日本語ラベル）とは別物なので注意。"ids"は1件のこともあれば、
下記のように1回の通知で複数レコードの変更をまとめて通知してくることもある（バッチ通知）。
その場合"affected_values"にも対応するレコードIDごとのエントリが複数含まれる:
{
  "server_time": 1718115953625,
  "affected_values": [
    {
      "record_id": "5725767000003000010",
      "values": {
        "field71": "商談中(B)"
      }
    },
    {
      "record_id": "5725767000003000020",
      "values": {
        "field71": "受注(Won)"
      }
    }
  ],
  "query_params": {},
  "module": "Deals",
  "resource_uri": "https://www.zohoapis.com/crm/v8/Leads",
  "ids": ["5725767000003000010", "5725767000003000020"],
  "affected_fields": [],
  "operation": "insert",
  "channel_id": "1000000068001",
  "token": "（scripts/register_zoho_webhook.pyが登録時に指定したtoken文字列がそのまま返る）"
}

旧実装は`{"data": [{"id": ..., "Modified_Time": ..., ...フィールド...}]}`という誤った
（未検証の想定に基づく）形状を前提に書かれており、実際の通知は上記の通り異なる形状のため
全件がHTTP 400で失敗していた（2026-08-12発覚）。実際には変更後のレコード全体ではなく、
"ids"（変更対象レコードIDの配列。1通知で複数件をまとめて通知しうる）と"affected_values"
（レコードIDごとの、変更されたフィールドのみのdelta。insert/delete等では空/欠落もありうる）
のみが渡される点、および1レコードあたりの更新日時（Modified_Time相当）は含まれず、通知全体
共通の"server_time"（UTCエポックミリ秒）のみが渡される点が異なる。"ids"の各要素は
zoho_payload_to_sync_events()（複数形。1通知1レコードのみを前提にしていたBLOCKER、
2026-08-12発覚・修正）によりそれぞれ独立したSyncEventへ変換され、handler()が1件ずつ
dispatchする。

"affected_values[*].values"のキーはZoho内部のフィールドapi_name（例: "field71"）であり、
DatabaseSchemaのプロパティ名として使う日本語ラベル（例: "営業ステータス"）とは別物。この変換は
本ハンドラでは行わず、config/zoho_field_mapping.json（scripts/fetch_zoho_field_mapping.pyが
実際のZoho API から取得・更新する静的データ）を参照する
src.sync_engine.zoho_field_mapping.resolve_zoho_field_label() に委譲する。

認証: Zoho Notifications（watch）APIは着信リクエストへ任意のHTTPヘッダーを付与させる仕組みを
持たないため、他ハンドラのようなverify_webhook_secret()（X-Webhook-Secretヘッダー方式）は
使えない。代わりに上記の通りbody内の"token"フィールドをZOHO_WEBHOOK_SECRETと照合する
verify_webhook_body_token()（_common.py）を使う。詳細はdocs/zoho_webhook_activation_note.md参照。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Mapping

from src.db_schema.base import Tool
from src.db_schema.registry import ALL_SCHEMAS, get_schema
from src.sync_engine.dispatcher import Dispatcher, DispatchResult
from src.sync_engine.sync_event import SyncEvent
from src.sync_engine.sync_headers import HEADER_NAME
from src.sync_engine.sync_targets.zoho_sync import is_zoho_enabled
from src.sync_engine.webhook_handlers._common import (
    bad_request_response,
    get_header,
    internal_error_response,
    logger,
    unauthorized_response,
    verify_webhook_body_token,
)
from src.sync_engine.zoho_field_mapping import resolve_zoho_field_label


def _default_module_to_db_key() -> dict[str, str]:
    # zoho_key（移行元Zohoモジュール名の日本語ラベル）ではなく、実際のZoho CRM APIの
    # module値と一致するzoho_api_moduleで逆引きする（BLOCKER4: zoho_keyは表示用ラベルであり
    # 実際のWebhookペイロードのmodule値とは一致しないため）。
    return {schema.zoho_api_module: schema.key for schema in ALL_SCHEMAS}


def _server_time_to_datetime(payload: Mapping[str, Any]) -> datetime:
    """通知全体共通の"server_time"（UTCエポックミリ秒）をdatetimeへ変換する。

    SyncEvent.occurred_atはあいまいさを避けるため必須（Noneを許容しない）。
    実際の本番Zoho Notifications通知には基本的に"server_time"が含まれるが、念のため
    欠落していた場合は「正確な発生時刻は不明だが、この時点で受信した」ことを表す値として
    受信時点の現在時刻をフォールバックに使う（kintone/notion等の他ハンドラは元ペイロードに
    必ずタイムスタンプがある前提のため、このフォールバックはzoho_webhook.py固有）。
    """
    server_time = payload.get("server_time")
    if server_time is None:
        return datetime.now(timezone.utc)
    return datetime.fromtimestamp(server_time / 1000, tz=timezone.utc)


def zoho_payload_to_sync_events(
    payload: Mapping[str, Any],
    headers: Mapping[str, str],
    *,
    module_to_db_key: Mapping[str, str] | None = None,
) -> list[SyncEvent]:
    """Zoho Notification WebhookペイロードをSyncEventのリストへ変換する。

    複数形なのは、Zoho側が1回の通知に複数レコードの変更をまとめて送ってくる
    （"ids"に複数件、"affected_values"にもレコードIDごとに対応するエントリが複数含まれる）
    ことがあり、そのすべてを取りこぼさず処理する必要があるため（2026-08-12発覚のBLOCKER:
    旧実装は`ids[0]`のみを見ており、バッチ通知の2件目以降が無条件に無視されデータ消失していた）。
    """
    resolver = module_to_db_key if module_to_db_key is not None else _default_module_to_db_key()
    module = payload["module"]
    db_key = resolver.get(module)
    if db_key is None:
        raise ValueError(f"unknown Zoho module: {module!r}")

    ids = payload.get("ids") or []
    if not ids:
        # "ids"が欠落している場合も空配列の場合も、同じ「対象レコードが特定できない」
        # エラーとして扱う（呼び出し元にとってどちらも同じ結果のため区別する意味が無い）。
        raise ValueError("zoho webhook payload has no ids")

    affected_values = payload.get("affected_values") or []
    schema = get_schema(db_key)
    occurred_at = _server_time_to_datetime(payload)
    sync_system_id = get_header(headers, HEADER_NAME)

    events: list[SyncEvent] = []
    for raw_id in ids:
        record_id = str(raw_id)

        # affected_valuesは「変更されたフィールドのみ」のdeltaで、1通知にids/affected_values
        # の組が複数含まれうるため、先頭を無条件に使わずrecord_idで対応するエントリを探す。
        # insert/delete等、対応するエントリが無い（=フィールド単位の変更が無い）場合は
        # 空のpropertiesとして扱う（エラーにしない）。
        matched_values: Mapping[str, Any] = {}
        for entry in affected_values:
            if isinstance(entry, Mapping) and str(entry.get("record_id")) == record_id:
                raw_values = entry.get("values")
                if isinstance(raw_values, Mapping):
                    matched_values = raw_values
                break

        # Dispatcher側（未定義プロパティのスキップ）で根本的には保護されるが、Zoho側でも
        # 早期に警告ログを出しておく（Notionのようなrollup/formula型の大量発生は想定しにくいため、
        # notion_webhook.pyのような型ホワイトリストまでは設けない簡易対応）。
        properties: dict[str, Any] = {}
        for api_name, value in matched_values.items():
            # api_name -> ラベルへ変換できない（マッピング未登録）場合と、変換できてもschema側に
            # 該当プロパティが無い場合を、同じ「未知のフィールドとしてスキップ」の警告ログへ合流させる
            # （呼び出し元にとってはどちらも「このフィールドは同期対象外」という同じ結果のため）。
            # このスキップは当該レコード（イベント）のみに閉じており、バッチ内の他レコードの
            # 処理には影響しない。
            label = resolve_zoho_field_label(module, api_name)
            if label is not None:
                try:
                    schema.get_property(label)
                except KeyError:
                    label = None
            if label is None:
                logger.warning(
                    "ignoring unknown Zoho property api_name='%s' for db_key=%r (not in schema)",
                    api_name,
                    db_key,
                )
                continue
            properties[label] = value

        events.append(
            SyncEvent(
                source_tool=Tool.ZOHO,
                db_key=db_key,
                external_id=record_id,
                occurred_at=occurred_at,
                properties=properties,
                sync_system_id=sync_system_id,
            )
        )

    return events


def handler(
    event: Mapping[str, Any], context: object, *, dispatcher: Dispatcher | None = None
) -> dict[str, Any]:
    """Lambda/Cloud Functions エントリポイント（API Gateway形式のHTTPイベントを想定）。

    実際のデプロイ設定（SAM/Serverless Framework等）は範囲外。ENABLE_ZOHO=False時は
    ペイロード変換・dispatcherへのdispatchのいずれも行わずスキップする。

    Zohoはtokenをbody内に返すため、認証（verify_webhook_body_token）にはbodyのJSONパースが
    必要になる。他ハンドラ（ヘッダー方式）と異なり、JSONパース失敗（400）はtoken検証（401）
    より先に判定される点に注意。
    """
    if not is_zoho_enabled():
        return {"statusCode": 200, "body": json.dumps({"skipped": "zoho_disabled"})}

    headers = event.get("headers") or {}

    try:
        body = event.get("body")
        payload = json.loads(body) if isinstance(body, str) else (body or {})
    except json.JSONDecodeError as exc:
        return bad_request_response(f"invalid JSON payload: {exc}")

    # BLOCKER2: 構文的には正しいJSONでも辞書でない場合（例: "null"/"[1,2,3]"/"42"/"\"x\""/"true"）、
    # 未認証の送信者がこの時点でverify_webhook_body_token()内のpayload.get()に到達すると
    # AttributeErrorが未捕捉のまま外へ漏れてしまうため、JSONパース失敗と同様に400へ倒す。
    if not isinstance(payload, dict):
        return bad_request_response("request body must be a JSON object")

    if not verify_webhook_body_token(payload, token_field="token", env_var="ZOHO_WEBHOOK_SECRET"):
        return unauthorized_response()

    try:
        sync_events = zoho_payload_to_sync_events(payload, headers)
    except (KeyError, ValueError) as exc:
        return bad_request_response(str(exc))
    except Exception:
        logger.exception("unexpected error while parsing zoho webhook payload")
        return internal_error_response()

    # バッチ通知（"ids"に複数件）は各SyncEventが独立したレコードの更新であり、1件の
    # dispatch失敗が他のレコードの同期を妨げるべきではないため、all-or-nothingにせず
    # 各イベントを独立にdispatchして結果を集める（1件目が成功し2件目が失敗しても、
    # 1件目の反映は取り消さない）。
    #
    # いずれかのdispatchで想定外の例外が起きた場合、そのイベントはエラーとして記録しつつ
    # 残りのイベントの処理は継続したうえで、HTTPレスポンス全体としては500を返しZohoの
    # リトライを促す。Dispatcher.dispatch()はevent.occurred_at <= mapping.last_synced_at
    # のイベントを"stale_event"としてスキップする冪等性を既に持つ（同一SyncEventの
    # 再dispatchは実害が無い）ため、バッチ全体をリトライさせても、既に成功した分が
    # 二重処理される心配はない。
    results: list[dict[str, Any]] = []
    had_unexpected_error = False
    for sync_event in sync_events:
        try:
            result: DispatchResult | None = (
                dispatcher.dispatch(sync_event) if dispatcher is not None else None
            )
        except Exception:
            logger.exception(
                "unexpected error while dispatching zoho sync event (external_id=%s)",
                sync_event.external_id,
            )
            had_unexpected_error = True
            continue
        results.append(
            {
                "external_id": sync_event.external_id,
                "skipped": result.skipped if result is not None else None,
            }
        )

    if had_unexpected_error:
        return internal_error_response()

    return {
        "statusCode": 200,
        "body": json.dumps({"results": results}),
    }
