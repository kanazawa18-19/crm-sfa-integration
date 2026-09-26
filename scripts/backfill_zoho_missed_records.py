#!/usr/bin/env python3
"""Zoho の通知（watch）が止まっていた間に Zoho 側で作られた（対応表に無い）レコードを、
Notion へ作り直すための回収スクリプト。**対象は「作成漏れ」だけ**。止まっていた間の
更新（対応表にあるレコード）と削除は回収しない（下記）。

■ 背景（2026-09-26）
Zoho CRM Notifications（watch）チャンネルは登録から最大 1 日で失効する。2026-09-16〜21 の
Vercel 停止（Hobby 上限・チーム移行）で失効し、以後 `PUT` での延長も効かず（チャンネルが
Zoho 側から消えていた）、**9/15 16:01 JST を最後に Zoho 発の同期が止まっていた**。
その間に Zoho へ CSV インポートされた連絡先など（対応表に無いレコード）は、購読を
再登録しても Zoho から改めて通知されることはないので、このスクリプトで拾う。

■ やること
    Zoho から「--since 以降に変更されたレコードの ID」を取得
        （v3 一覧 API。v2 の page 方式の 2,000 件上限を避け、page_token で最大 100,000 件まで）
        ──▶ 対応表（IdMapping）に無いもの ＝ 取りこぼし（新規）
        ──▶ dry-run: 1 件ずつ Zoho から取り直して「作れるか」を予測（読み取りだけ）
        ──▶ --apply: 本番と同じ Dispatcher に「新規レコードの通知」として流す
            （AUTO_CREATE_NEW_RECORDS_ENABLED の経路。Notion ページ作成＋対応表登録。
              予測で事前に絞らず全件流し、作れるかは本番と同じ Dispatcher が決める）

対応表に**ある**レコード（＝以前同期済みで、その後 Zoho 側で変更されたもの）は今回は触らない。
Webhook の「変更差分」が無いので全項目を上書きすることになり、Notion 側で後から直した値を
Zoho の古い値で潰しうるため。件数と ID は報告だけする。
止まっていた間に Zoho 側で**削除**されたレコードも扱わない（一覧 API は現存レコードしか返さない。
Notion に残った側は relation-sync-reconcile 等の突き合わせに任せる）。

■ 使い方（既定は dry-run。本番 Notion への書き込みは --apply のときだけ）
    .venv/bin/python scripts/backfill_zoho_missed_records.py --since 2026-09-15T16:00+09:00
    .venv/bin/python scripts/backfill_zoho_missed_records.py --since 2026-09-15T16:00+09:00 --module Contacts
    .venv/bin/python scripts/backfill_zoho_missed_records.py --since ... --apply [--limit 5]

■ 環境変数（`config/.env`、.gitignore 済み）
    利用者が用意するもの:
      NOTION_API_KEY / ZOHO_CLIENT_ID / ZOHO_CLIENT_SECRET / ZOHO_REFRESH_TOKEN（必須）
      DATABASE_URL（連絡先の「お取引先」→ Notion 取引先マスターの解決に使う `ClientNameIndex` の置き場。
                    --apply では必須。dry-run では無くても動くが、取引先の解決は「未検証」になる）
      DATABASE_URL_UNPOOLED（--apply で推奨。シートの行をその場で作るのに要る直接接続。無いと行は
                    再試行キュー経由になり、日次 cron が 1 回 200 件ずつ作る）
    このスクリプトが強制的に上書きするもの（.env の値は見ない）:
      SYNC_ID_MAPPING_BACKEND=notion / ENABLE_ZOHO=True /
      AUTO_CREATE_NEW_RECORDS_ENABLED / RELATION_SYNC_ENABLED / SPREADSHEET_ROW_CREATION_ENABLED（--apply のみ true）

■ 安全側の設計
- 対応表は本番と同じ Notion 実装（`SYNC_ID_MAPPING_BACKEND=notion`）を強制する。SQLite 既定のまま
  動かすと「全部が未知レコード」に見えて二重作成するため、スクリプト内で固定し、起動時に実装名も確かめる。
- Slack 通知は切る（数百件を流すと必須項目不足のたびに DM が飛ぶ）。結果はこのスクリプトが
  まとめて標準出力と JSON に出す。
- dry-run では取引先の解決を**読み取りだけ**で試す（`RELATION_SYNC_ENABLED` を立てないので、
  本番の解決処理が持つ「レビューキューへの記録」という書き込みも起きない）。DB に繋げなかった
  レコードは「未検証」として数え、他のレコードの判定には影響させない。
- `--apply` では 1 件ずつ Dispatcher に流し、失敗しても残りを続ける（Webhook のバッチ通知と同じ方針）。
  ただし終了コードは結果で変える（error があれば 1、Dispatcher がスキップした件があれば 3、全部作成なら 0）。
  「続行する」と「成功として終わる」は別。
- 購読を戻した後に流すと、本番の Webhook と同じレコードを同時に扱いうる。Dispatcher 側が作成直前に
  対応表を引き直す（`_try_create_new_record`）のに加え、このスクリプトも各件の直前に対応表を引き直して
  既に載っていれば流さない（`mapped_meanwhile`）。再実行しても、作成済みのものは対象から外れる。
- dry-run の予測は「本番の変換＋連絡先の取引先だけ読み取り専用で補う」もので、本番と同じ判定の再現ではない。
  他モジュールに必須のリレーションがあれば「必須項目不足」が実際より多く出る。--apply の可否には影響しない。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

logger = logging.getLogger("backfill_zoho_missed_records")

REQUIRED_ENV = ("NOTION_API_KEY", "ZOHO_CLIENT_ID", "ZOHO_CLIENT_SECRET", "ZOHO_REFRESH_TOKEN")
ZOHO_PAGE_SIZE = 200
# Zoho 連絡先の「お取引先」ルックアップ項目の API 名（ラベルとの対応は config/zoho_field_mapping.json）
ZOHO_CONTACT_ACCOUNT_LOOKUP_FIELD = "field25"

# 予測の種類
PREDICT_CREATE = "create"  # 必須項目が揃っており作れる見込み
PREDICT_MISSING_REQUIRED = "missing_required"  # 必須項目不足で Dispatcher がスキップする見込み
PREDICT_UNVERIFIED = "unverified"  # 取引先の解決を確かめられなかった（DATABASE_URL 無し／DB エラー）
PREDICT_SKIP_MAPPED = "skip_mapped"  # 対応表にある既存レコード（今回は触らない）
PREDICT_FETCH_FAILED = "fetch_failed"  # Zoho から取り直せなかった


# ---------------------------------------------------------------------------------------------
# 純粋な判定（テスト対象）
# ---------------------------------------------------------------------------------------------


@dataclass
class Classified:
    """Zoho レコード 1 件の分類結果。"""

    zoho_id: str
    db_key: str
    mapped: bool
    label: str = ""  # 表示用（氏名・会社名など。dry-run で取り直したときに埋まる）
    notion_key: str | None = None
    predicted: str = ""  # PREDICT_* のいずれか（dry-run で埋まる）
    missing_required: list[str] = field(default_factory=list)
    dispatch_reason: str | None = None  # --apply の結果（"created" / Dispatcher の skip 理由 / "error"）
    error: str | None = None


def split_by_mapping(zoho_ids: Iterable[str], mapped_ids: Mapping[str, str], db_key: str) -> list[Classified]:
    """対応表に載っている zoho_id の集合（zoho_id → notion_key）で、Zoho レコードを
    「新規（対応表に無い）」と「既存（対応表にある）」に分ける。"""
    out: list[Classified] = []
    for raw in zoho_ids:
        zid = str(raw)
        notion_key = mapped_ids.get(zid)
        out.append(
            Classified(
                zoho_id=zid,
                db_key=db_key,
                mapped=notion_key is not None,
                notion_key=notion_key,
                predicted=PREDICT_SKIP_MAPPED if notion_key is not None else "",
            )
        )
    return out


def missing_required_properties(db_key: str, properties: Mapping[str, Any]) -> list[str]:
    """Dispatcher._try_create_new_record と同じ基準で、必須プロパティの不足を列挙する。"""
    from src.db_schema.registry import get_schema
    from src.sync_engine.dispatcher import _is_missing_required_value

    schema = get_schema(db_key)
    return [
        prop.name
        for prop in schema.properties
        if prop.is_required and _is_missing_required_value(properties.get(prop.name))
    ]


def parse_since(value: str) -> datetime:
    """`--since` は ISO 8601（タイムゾーン必須）。Zoho の If-Modified-Since ヘッダーへそのまま渡す。"""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise argparse.ArgumentTypeError("--since にはタイムゾーンを付けてください（例: 2026-09-15T16:00+09:00）")
    return dt


def zoho_record_label(rec: Mapping[str, Any]) -> str:
    name = rec.get("Full_Name") or rec.get("Deal_Name") or rec.get("Account_Name") or rec.get("Name") or ""
    if isinstance(name, Mapping):
        name = name.get("name", "")
    account = rec.get(ZOHO_CONTACT_ACCOUNT_LOOKUP_FIELD) or rec.get("Account_Name")
    if isinstance(account, Mapping):
        account = account.get("name")
    return f"{name} | {account or '-'}"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--since", required=True, type=parse_since, help="この日時以降に Zoho で変更されたレコードを対象にする（ISO 8601、TZ 必須）")
    parser.add_argument("--module", action="append", dest="modules", help="Zoho モジュール API 名（複数可）。省略時は購読している 6 モジュール全部")
    parser.add_argument("--apply", action="store_true", help="実際に Notion へ作成する（省略時は dry-run）")
    parser.add_argument("--limit", type=int, default=None, help="対象にする新規レコードの上限（最初の N 件。dry-run の予測にも --apply にも効く）")
    parser.add_argument("--report-dir", default=None, help="結果 JSON の保存先（既定: ~/.local/state/crm-sfa-zoho-backfill-<日付>/）")
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------------------------
# 環境・クライアント
# ---------------------------------------------------------------------------------------------


def load_env(apply: bool, env_path: Path | None = None) -> None:
    """`config/.env` を読み（既にシェルにある値を優先）、このスクリプト用に固定する値を上書きする。"""
    env_path = env_path or (REPO / "config" / ".env")
    if not env_path.exists():
        print(f"エラー: {env_path} がありません（認証情報の置き場所）", file=sys.stderr)
        raise SystemExit(2)
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"'))
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        print(f"エラー: 環境変数が足りません: {', '.join(missing)}", file=sys.stderr)
        raise SystemExit(2)
    # 本番と同じ対応表（Notion）を強制。SQLite 既定のままだと全件が「未知」に見えて二重作成する
    os.environ["SYNC_ID_MAPPING_BACKEND"] = "notion"
    os.environ.setdefault("SYNC_ID_MAPPING_NOTION_API_KEY", os.environ["NOTION_API_KEY"])
    os.environ["ENABLE_ZOHO"] = "True"
    if apply:
        os.environ["AUTO_CREATE_NEW_RECORDS_ENABLED"] = "true"
        # 取引先の解決（ClientNameIndex）は RELATION_SYNC_ENABLED=true のときだけ動く。
        # 未解決のレコードはレビューキュー（Postgres）へ記録される（本番 Webhook と同じ）。
        os.environ["RELATION_SYNC_ENABLED"] = "true"
        if not os.environ.get("DATABASE_URL"):
            print("エラー: --apply には DATABASE_URL が必要です（取引先の解決に使う ClientNameIndex の置き場）", file=sys.stderr)
            raise SystemExit(2)
        # シート（連絡先タブ等）の行もその場で作る（scripts/backfill_spreadsheet_all.py と同じ）。
        # 行作成の前に「同期状態の確認」が走り、これには直接接続 DATABASE_URL_UNPOOLED が要る。
        # 無いと行作成は再試行キュー（SpreadsheetOutbox）に積まれ、日次 cron が 1 回 200 件ずつ作る
        # （数百件だと数日かかる）ので、無ければ起動時に知らせる。
        os.environ["SPREADSHEET_ROW_CREATION_ENABLED"] = "true"
        os.environ.setdefault("SPREADSHEET_ROW_CREATION_DB_KEYS", "*")
        if not os.environ.get("DATABASE_URL_UNPOOLED"):
            print("⚠️  DATABASE_URL_UNPOOLED が無いので、シートの行はその場で作れず再試行キュー経由（日次 cron・1 回 200 件）になる", file=sys.stderr)
    else:
        # dry-run ではレビューキューへの書き込みを起こさない
        os.environ["RELATION_SYNC_ENABLED"] = "false"


def modules_to_db_keys(modules: Sequence[str] | None) -> dict[str, str]:
    from src.db_schema.registry import SCHEMAS_BY_KEY
    from src.sync_engine.zoho_watch_channel import DEFAULT_MODULES

    by_module = {s.zoho_api_module: s.key for s in SCHEMAS_BY_KEY.values() if s.zoho_api_module}
    wanted = list(modules) if modules else list(DEFAULT_MODULES)
    unknown = [m for m in wanted if m not in by_module]
    if unknown:
        raise SystemExit(f"エラー: 対応表に無いモジュール: {unknown}（使えるもの: {sorted(by_module)}）")
    return {m: by_module[m] for m in wanted}


def zoho_v3_base_url() -> str:
    """CRUD 用の `ZOHO_API_BASE_URL`（`/crm/v2` 固定）から、一覧 API 用の `/crm/v3` を組み立てる。"""
    v2 = os.environ.get("ZOHO_API_BASE_URL", "https://www.zohoapis.jp/crm/v2").rstrip("/")
    return v2[: -len("/v2")] + "/v3" if v2.endswith("/v2") else v2


def fetch_zoho_ids_modified_since(client, module: str, since: datetime) -> list[str]:
    """`GET /crm/v3/{module}?fields=id` を If-Modified-Since で絞り、`page_token` で全件たどる
    （読み取りだけ。v2 の `page` 方式は先頭 2,000 件までしか許されないので使わない）。
    Zoho は該当なしのとき 304 または 204 を返す。認証・429/5xx リトライは `HttpZohoClient.request()` に任せる。"""
    from src.sync_engine.clients._http import raise_for_error
    from src.sync_engine.clients.zoho_client import ZohoApiError

    base = zoho_v3_base_url()
    ids: list[str] = []
    page_token: str | None = None
    while True:
        params: dict[str, Any] = {"fields": "id", "per_page": ZOHO_PAGE_SIZE}
        if page_token:
            params["page_token"] = page_token
        url = f"{base}/{module}?{urlencode(params)}"
        response = client.request("GET", url, idempotent=True)
        if response.status_code in (204, 304):
            break
        raise_for_error(response, ZohoApiError)
        body = response.json()
        ids.extend(str(r["id"]) for r in body.get("data", []))
        info = body.get("info") or {}
        page_token = info.get("next_page_token")
        if not info.get("more_records") or not page_token:
            break
        time.sleep(0.2)
    return ids


def _with_if_modified_since(client, since: datetime):
    """`HttpZohoClient.request()` は追加ヘッダーを受け取らないので、`_headers()` を包んで
    If-Modified-Since を足す（このスクリプトの中だけ。呼び出し後は元に戻す）。"""
    original = client._headers

    def patched() -> dict[str, str]:
        # 秒精度に丸める（マイクロ秒付きだと Zoho が ISO 8601 として受け付けない恐れ。Gemini レビュー WARN）
        return {**original(), "If-Modified-Since": since.isoformat(timespec="seconds")}

    client._headers = patched
    return original


def load_mapped_zoho_ids(store, db_key: str) -> dict[str, str]:
    """対応表の当該 db_key を全件読み、zoho_id → notion_key の辞書にする（1 件ずつ検索するより速い）。"""
    return {m.zoho_id: m.notion_key for m in store.list_by_db(db_key) if m.zoho_id}


# ---------------------------------------------------------------------------------------------
# dry-run の予測（読み取りだけ）
# ---------------------------------------------------------------------------------------------


def lookup_client_master_readonly(lookup_value: Any) -> tuple[str | None, bool]:
    """連絡先の「お取引先」ルックアップ（{"name":..., "id":...}）を、本番と同じ正規化＋完全一致で
    取引先マスターの Notion page ID に引く。戻り値は (page_id または None, 検証できたか)。
    DATABASE_URL が無い／DB エラーのときは (None, False)。状態は持たず、毎回引き直す。"""
    if not os.environ.get("DATABASE_URL"):
        return None, False
    from src.relation_sync.db import find_by_normalized_name
    from src.relation_sync.resolve import normalize_company_name_strong
    from src.sync_engine.webhook_handlers.zoho_field_transforms import extract_zoho_lookup_name

    name = extract_zoho_lookup_name(lookup_value)
    if not name or not str(name).strip():
        return None, True  # 未入力＝解決対象外（検証はできている）
    try:
        matches = find_by_normalized_name(normalize_company_name_strong(str(name)))
    except Exception as exc:  # DB に繋げない等。他のレコードの判定には影響させない
        logger.warning("ClientNameIndex を引けませんでした（このレコードは未検証扱い）: %r", exc)
        return None, False
    return (matches[0]["notion_page_id"] if len(matches) == 1 else None), True


def predict_creation(
    item: Classified,
    raw_record: Mapping[str, Any],
    store,
    *,
    lookup: Callable[[Any], tuple[str | None, bool]] = lookup_client_master_readonly,
) -> None:
    """dry-run 専用。本番の変換（`build_notion_properties_for_new_record`）を通し、必須不足を判定する。
    dry-run では RELATION_SYNC_ENABLED=false なので「お取引先」は必ず未解決になる。そこで
    取引先だけは ClientNameIndex を読み取りだけで引き直して埋め、実際に作れるかを予測する。
    --apply ではこの関数を使わず、本番と同じ Dispatcher に判断させる。"""
    from src.db_schema.base import Tool
    from src.sync_engine.new_record_builder import build_notion_properties_for_new_record

    item.label = zoho_record_label(raw_record)
    props = build_notion_properties_for_new_record(
        source_tool=Tool.ZOHO, db_key=item.db_key, external_id=item.zoho_id, raw_record=raw_record, id_mapping_store=store
    )
    verified = True
    if item.db_key == "contact" and not props.get("取引先マスター"):
        resolved, verified = lookup(raw_record.get(ZOHO_CONTACT_ACCOUNT_LOOKUP_FIELD))
        if resolved is not None:
            props["取引先マスター"] = resolved
    missing = missing_required_properties(item.db_key, props)
    item.missing_required = missing
    if not missing:
        item.predicted = PREDICT_CREATE
    elif not verified and "取引先マスター" in missing:
        item.predicted = PREDICT_UNVERIFIED
    else:
        item.predicted = PREDICT_MISSING_REQUIRED


# ---------------------------------------------------------------------------------------------
# 本体
# ---------------------------------------------------------------------------------------------


def collect(zoho, store, modules: Mapping[str, str], since: datetime) -> tuple[list[Classified], dict[str, Any]]:
    """モジュールごとに Zoho の変更 ID と対応表を突き合わせ、全件の分類と集計を返す（読み取りだけ）。"""
    items: list[Classified] = []
    summary: dict[str, Any] = {}
    original_headers = _with_if_modified_since(zoho, since)
    try:
        for module, db_key in modules.items():
            ids = fetch_zoho_ids_modified_since(zoho, module, since)
            mapped = load_mapped_zoho_ids(store, db_key)
            classified = split_by_mapping(ids, mapped, db_key)
            new = [it for it in classified if not it.mapped]
            print(f"[{module} → {db_key}] Zoho 変更 {len(ids)} 件 / 対応表 {len(mapped)} 件 / "
                  f"対応表に無い（新規）{len(new)} 件 / 既存・変更あり（今回は触らない）{len(classified) - len(new)} 件")
            summary[module] = {
                "db_key": db_key, "zoho_modified": len(ids), "mapped_total": len(mapped),
                "new": len(new), "existing_modified": len(classified) - len(new),
                "existing_modified_ids": [it.zoho_id for it in classified if it.mapped],
            }
            items.extend(classified)
    finally:
        zoho._headers = original_headers
    return items, summary


def dry_run_predict(zoho, store, new_items: Sequence[Classified]) -> None:
    """新規レコードを 1 件ずつ Zoho から取り直し（本番の作成経路と同じ単一レコード取得）、作れるかを予測する。"""
    from src.db_schema.registry import get_schema

    total = len(new_items)
    for i, it in enumerate(new_items, 1):
        module = get_schema(it.db_key).zoho_api_module
        try:
            raw = zoho.get_record(module, it.zoho_id)
        except Exception as exc:
            it.predicted = PREDICT_FETCH_FAILED
            it.error = repr(exc)[:300]
            continue
        if raw is None:
            it.predicted = PREDICT_FETCH_FAILED
            it.error = "not found"
            continue
        predict_creation(it, raw, store)
        if i % 50 == 0 or i == total:
            print(f"   予測 {i}/{total} 件", flush=True)
        time.sleep(0.2)


def apply_all(dispatcher, new_items: Sequence[Classified], *, store=None, sleep_seconds: float = 0.4) -> Counter:
    """新規レコードを本番と同じ Dispatcher に「新規レコードの通知」として 1 件ずつ流す。
    予測で事前に絞らない（作れるかは本番と同じ Dispatcher が決める）。1 件の失敗で止めない。
    `store` を渡すと、各件の直前に対応表を引き直し、収集後に本番 Webhook が作成済みなら流さない。"""
    from src.audit_log.actor_context import set_actor
    from src.db_schema.base import Tool
    from src.sync_engine.sync_event import SyncEvent

    now = datetime.now(timezone.utc)
    total = len(new_items)
    for i, it in enumerate(new_items, 1):
        if store is not None:
            try:
                already = store.find_by_external_id(Tool.ZOHO, it.zoho_id, db_key=it.db_key)
            except Exception as exc:
                already = None
                logger.warning("対応表の引き直しに失敗（Dispatcher 側の再確認に任せる）: %r", exc)
            if already is not None:
                it.dispatch_reason = "mapped_meanwhile"
                it.notion_key = already.notion_key
                print(f"  [{i}/{total}] {it.db_key} {it.zoho_id} → mapped_meanwhile（収集後に作成済み）", flush=True)
                continue
        event = SyncEvent(source_tool=Tool.ZOHO, db_key=it.db_key, external_id=it.zoho_id, occurred_at=now, properties={})
        try:
            with set_actor("zoho_backfill"):
                result = dispatcher.dispatch(event)
            it.dispatch_reason = result.reason if result.skipped else "created"
        except Exception as exc:
            it.dispatch_reason = "error"
            it.error = repr(exc)[:300]
            logger.exception("dispatch failed for zoho_id=%s", it.zoho_id)
        print(f"  [{i}/{total}] {it.db_key} {it.zoho_id} → {it.dispatch_reason}", flush=True)
        if sleep_seconds:
            time.sleep(sleep_seconds)
    return Counter(it.dispatch_reason for it in new_items)


def run(args: argparse.Namespace) -> int:
    load_env(apply=args.apply)
    from src.sync_engine.production_wiring import build_id_mapping_store, build_production_dispatcher
    from src.sync_engine.zoho_watch_channel import build_zoho_client_from_env

    mode = "APPLY（本番 Notion へ書き込み）" if args.apply else "DRY-RUN（読み取りだけ）"
    print(f"== {mode} since={args.since.isoformat()} ==")
    zoho = build_zoho_client_from_env()
    store = build_id_mapping_store()
    if type(store).__name__ != "NotionIdMappingStore":
        raise SystemExit(f"エラー: 対応表が本番と違う実装です（{type(store).__name__}）")

    items, summary = collect(zoho, store, modules_to_db_keys(args.modules), args.since)
    new_items = [it for it in items if not it.mapped]
    if args.limit is not None:
        new_items = new_items[: args.limit]
    report: dict[str, Any] = {"mode": "apply" if args.apply else "dry-run", "since": args.since.isoformat(), "modules": summary}

    if args.apply:
        dispatcher = build_production_dispatcher(id_mapping_store=store, zoho_client=zoho, slack_notifier=None)
        print(f"== {len(new_items)} 件を Dispatcher に流す ==")
        counts = apply_all(dispatcher, new_items, store=store)
        print("== 結果:", dict(counts))
        exit_code = exit_code_for(counts)
    else:
        print(f"== 新規 {len(new_items)} 件を 1 件ずつ取り直して予測（読み取りだけ）==")
        dry_run_predict(zoho, store, new_items)
        counts = Counter(it.predicted for it in new_items)
        print(f"== 予測: 作成できる {counts.get(PREDICT_CREATE, 0)} / 必須項目不足 {counts.get(PREDICT_MISSING_REQUIRED, 0)} / "
              f"未検証 {counts.get(PREDICT_UNVERIFIED, 0)} / 取り直し失敗 {counts.get(PREDICT_FETCH_FAILED, 0)}")
        reasons = Counter(tuple(it.missing_required) for it in new_items if it.predicted == PREDICT_MISSING_REQUIRED)
        for k, v in reasons.most_common(5):
            print(f"     不足 {list(k)}: {v} 件")
        if counts.get(PREDICT_UNVERIFIED):
            print("⚠️  「未検証」は DATABASE_URL が無い／DB に繋げず、取引先マスターの解決を確かめられなかった件数。"
                  "--apply では本番と同じ経路で解決を試みるので、実際にはこのうち一部は作成できる")

    if not args.apply:
        exit_code = 0
    report["items"] = [asdict(it) for it in new_items]
    out_dir = Path(args.report_dir) if args.report_dir else Path.home() / ".local" / "state" / f"crm-sfa-zoho-backfill-{datetime.now().strftime('%Y%m%d')}"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{'apply' if args.apply else 'dry-run'}-{datetime.now().strftime('%H%M%S')}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"結果 JSON: {out}")
    if exit_code:
        print(f"⚠️  終了コード {exit_code}（1=error あり / 3=Dispatcher がスキップした件あり）。結果 JSON で対象を確認すること")
    return exit_code


def exit_code_for(counts: Mapping[str, int]) -> int:
    """--apply の終了コード。0=全件作成（または収集後に作成済み）/ 1=error あり / 3=Dispatcher がスキップした件あり。"""
    if counts.get("error"):
        return 1
    if any(reason not in ("created", "mapped_meanwhile") for reason in counts):
        return 3
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    raise SystemExit(run(parse_args()))
