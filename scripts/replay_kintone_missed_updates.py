#!/usr/bin/env python3
"""kintone 側で直したのに Notion に届いていない更新を、Notion / Zoho / シートへ流し直す（既定 dry-run）。

■ 背景
2026-08-31〜09-27 05:57 JST は kintone 発の既存レコード更新が Notion に書く直前に TypeError で落ちていた。
洗い出しは `inventory_kintone_missed_updates.py`（読み取りだけ）。このスクリプトはそれと同じ分類を行い、
`--apply` のときだけ「safe な項目」を本番と同じ Dispatcher に kintone の通知として流す。

■ 流し方（`replay_zoho_missed_updates.py` と同じ考え方）
- safe（Notion ページが kintone の更新より後に触られていない項目）だけを流す。ambiguous・whitespace_only は流さない
- イベント時刻は「今」。kintone の更新時刻のままだと、対応表の最終同期時刻より古くて stale_event になる
- **書く直前に kintone レコードと Notion ページを読み直し**、分類したときから動いていたら流さない
  （`kintone_edited_after_plan` / `notion_edited_after_plan`。再実行すれば分類し直される）
- `--exclude-kintone-id` で、kintone 側が壊れているレコード（例: 取引先マスター #62388）を外せる
- Slack 通知は切る。競合で捨てた値は標準出力と結果 JSON に残す

■ 使い方
    .venv/bin/python scripts/replay_kintone_missed_updates.py \\
        --since 2026-08-31T00:00+09:00 --until 2026-09-27T05:57+09:00 --db-key client_master
    ... --apply [--limit 3] [--exclude-kintone-id 62388]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

import backfill_zoho_missed_records as backfill  # noqa: E402
import inventory_kintone_missed_updates as inv_mod  # noqa: E402

logger = logging.getLogger("replay_kintone")

REPLAY_ACTOR = "kintone_replay"
OUTCOME_UPDATED = "updated"
OUTCOME_LIMIT_SKIPPED = "not_applied_by_limit"


# ---------------------------------------------------------------------------------------------
# 純粋な部分
# ---------------------------------------------------------------------------------------------


def safe_properties(inv: inv_mod.RecordInventory) -> dict[str, Any]:
    """流す対象（safe な項目）の Notion プロパティ名 → kintone の値。"""
    return {f.notion_property: f.kintone_value for f in inv.with_status(inv_mod.FIELD_SAFE)}


def exit_code_for(records: Sequence[inv_mod.RecordInventory], *, apply: bool) -> int:
    """1=error あり / 3=--apply で流せなかった件あり / 0=それ以外（needs_review は正常な判定結果）。"""
    if any(r.outcome == inv_mod.RECORD_ERROR for r in records):
        return 1
    if not apply:
        return 0
    ok = (inv_mod.RECORD_NOTHING_TO_DO, inv_mod.RECORD_NEEDS_REVIEW, inv_mod.RECORD_NOT_MAPPED, OUTCOME_UPDATED, OUTCOME_LIMIT_SKIPPED)
    return 3 if any(r.outcome not in ok for r in records) else 0


def values_equal_or_both_empty(a: Any, b: Any) -> bool:
    from src.sync_engine.conflict_resolver import _is_empty, _values_equal

    def empty(v: Any) -> bool:
        return _is_empty(v) or v == []

    return _values_equal(a, b) or (empty(a) and empty(b))


# ---------------------------------------------------------------------------------------------
# 書く直前の読み直し
# ---------------------------------------------------------------------------------------------


def fetch_kintone_record(db_key: str, kintone_id: str) -> dict[str, Any] | None:
    from src.sync_engine.clients._http import request_with_retry

    domain = os.environ["KINTONE_DOMAIN"]
    url = (domain if domain.startswith("http") else f"https://{domain}") + "/k/v1/record.json"
    app, token = inv_mod.kintone_app_and_token(db_key)  # type: ignore[misc]
    resp = request_with_retry("GET", url, headers={"X-Cybozu-API-Token": token}, params={"app": app, "id": kintone_id}, timeout=60)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()["record"]


def notion_page_unchanged_since_plan(inv: inv_mod.RecordInventory, notion_client) -> bool:
    """Notion ページの最終更新と、流す項目の値が分類時から動いていないこと（読めなければ流さない）。"""
    from src.sync_engine.clients._notion_keys import NOTION_LAST_EDITED_TIME_KEY

    if inv.notion_last_edited_at is None or inv.notion_key is None:
        return False
    page = notion_client.get_page(inv.notion_key)
    if page is None:
        return False
    last_edited = page.get(NOTION_LAST_EDITED_TIME_KEY)
    if not isinstance(last_edited, datetime) or last_edited > datetime.fromisoformat(inv.notion_last_edited_at):
        return False
    return all(values_equal_or_both_empty(page.get(f.notion_property), f.notion_value) for f in inv.with_status(inv_mod.FIELD_SAFE))


def apply_record(inv: inv_mod.RecordInventory, *, app_id: str, dispatcher, notion_client, store, now: datetime) -> None:
    from src.audit_log.actor_context import set_actor
    from src.sync_engine.webhook_handlers.kintone_webhook import kintone_payload_to_sync_event

    wanted = safe_properties(inv)
    try:
        raw = fetch_kintone_record(inv.db_key, inv.kintone_id)
        if raw is None or raw["更新日時"]["value"] != inv.updated_at.replace("+00:00", "Z"):
            inv.outcome = "kintone_edited_after_plan"
            return
        if not notion_page_unchanged_since_plan(inv, notion_client):
            inv.outcome = "notion_edited_after_plan"
            return
        event = kintone_payload_to_sync_event(
            {"app": {"id": app_id}, "record": raw}, {},
            app_id_to_db_key={app_id: inv.db_key}, id_mapping_store=store, notion_client=notion_client,
        )
        props = {k: v for k, v in event.properties.items() if k in wanted}
        if not props:
            inv.outcome = "nothing_converted"
            return
        with set_actor(REPLAY_ACTOR):
            result = dispatcher.dispatch(replace(event, occurred_at=now, properties=props))
        if result.skipped:
            inv.outcome = result.reason or "skipped"
            return
        inv.outcome = OUTCOME_UPDATED
        inv.written = {p.property_name: sorted(t.value for t in p.written_tools) for p in result.properties}
        inv.rejected = [
            {"property": p.property_name, "adopted_tool": r.adopted_tool.value, "adopted": r.adopted_value,
             "rejected_tool": r.rejected_tool.value, "rejected": r.rejected_value}
            for p in result.properties if p.resolution is not None for r in p.resolution.rejected
        ]
    except Exception as exc:
        inv.outcome = inv_mod.RECORD_ERROR
        inv.error = repr(exc)[:300]
        logger.exception("dispatch failed for %s %s", inv.db_key, inv.kintone_id)


# ---------------------------------------------------------------------------------------------
# 本体
# ---------------------------------------------------------------------------------------------


def parse_args(argv=None) -> argparse.Namespace:
    from src.sync_engine.production_wiring import _KINTONE_DB_ENV_SUFFIX

    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--since", required=True, type=backfill.parse_since)
    p.add_argument("--until", required=True, type=backfill.parse_since)
    p.add_argument("--db-key", dest="db_keys", action="append", choices=sorted(_KINTONE_DB_ENV_SUFFIX))
    p.add_argument("--kintone-id", dest="kintone_ids", action="append", type=inv_mod.kintone_id_arg,
                   help="このレコード番号だけを見る（複数可。--db-key と併用）")
    p.add_argument("--exclude-kintone-id", dest="exclude_ids", action="append", default=[], type=inv_mod.kintone_id_arg,
                   help="このレコード番号は流さない（複数可。kintone 側が壊れているものなど）")
    p.add_argument("--limit", type=int, default=None, help="流す候補（ready）の先頭 N 件だけ --apply する（残りは not_applied_by_limit。終了コードには影響しない）")
    p.add_argument("--apply", action="store_true", help="本番へ書き込む（省略時は読み取りだけ）")
    p.add_argument("--report-dir", default=None)
    return p.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    backfill.load_env(apply=args.apply)
    from src.sync_engine.production_wiring import (
        _KINTONE_DB_ENV_SUFFIX, build_id_mapping_store, build_notion_clients_by_db, build_production_dispatcher,
    )
    from src.sync_engine.zoho_watch_channel import build_zoho_client_from_env

    print(f"== {'APPLY（本番へ書き込み）' if args.apply else 'DRY-RUN（読み取りだけ）'} since={args.since.isoformat()} until={args.until.isoformat()} ==")
    store = build_id_mapping_store()
    if type(store).__name__ != "NotionIdMappingStore":
        raise SystemExit(f"エラー: 対応表が本番と違う実装です（{type(store).__name__}）")
    notion_clients = build_notion_clients_by_db()
    if not notion_clients:
        raise SystemExit("エラー: Notion クライアントを作れません")

    items: list[tuple[inv_mod.RecordInventory, dict[str, Any], str]] = []
    for db_key in args.db_keys or sorted(_KINTONE_DB_ENV_SUFFIX):
        creds = inv_mod.kintone_app_and_token(db_key)
        if creds is None:
            print(f"  {db_key}: kintone の環境変数が無いので飛ばす")
            continue
        if db_key not in notion_clients:
            raise SystemExit(f"エラー: {db_key} 用の Notion クライアントがありません")
        raws = inv_mod.fetch_updated_records(db_key, args.since, args.until, args.kintone_ids)
        print(f"  {db_key}: 期間内に更新されたレコード {len(raws)} 件")
        for raw in raws:
            kid = raw["$id"]["value"]
            if kid in args.exclude_ids:
                print(f"    #{kid} は --exclude-kintone-id で除外")
                continue
            items.append((inv_mod.RecordInventory(db_key=db_key, kintone_id=kid), raw, creds[0]))

    # 分類は読み取りだけ。--apply でも名寄せ（確認待ちキューへの書き込み）は流すレコードだけに限る
    relation_sync_for_apply = os.environ.get("RELATION_SYNC_ENABLED", "false")
    os.environ["RELATION_SYNC_ENABLED"] = "false"
    print(f"== {len(items)} 件を分類 ==")
    for i, (inv, raw, app_id) in enumerate(items, 1):
        inv_mod.plan_record(inv, raw, app_id=app_id, since=args.since, store=store, notion_client=notion_clients[inv.db_key])
        inv_mod.print_record(i, len(items), inv)
        time.sleep(0.2)

    records = [inv for inv, _, _ in items]
    if args.apply:
        os.environ["RELATION_SYNC_ENABLED"] = relation_sync_for_apply
        zoho = build_zoho_client_from_env()
        dispatcher = build_production_dispatcher(id_mapping_store=store, zoho_client=zoho, slack_notifier=None)
        targets = [(inv, app_id) for inv, _, app_id in items if inv.outcome == inv_mod.RECORD_READY]
        if args.limit is not None:
            for inv, _ in targets[args.limit:]:
                inv.outcome = OUTCOME_LIMIT_SKIPPED  # 分類は済んでいるが --limit のため今回は流さない
            targets = targets[: args.limit]
        print(f"== {len(targets)} 件を Dispatcher に流す ==")
        for i, (inv, app_id) in enumerate(targets, 1):
            apply_record(inv, app_id=app_id, dispatcher=dispatcher, notion_client=notion_clients[inv.db_key], store=store, now=datetime.now(timezone.utc))
            print(f"  [{i}/{len(targets)}] {inv.db_key} #{inv.kintone_id} → {inv.outcome} {getattr(inv, 'written', '') or ''}", flush=True)
            for rej in getattr(inv, "rejected", []):
                print(f"        競合で捨てた値: {rej['property']} {rej['rejected_tool']}={rej['rejected']!r} → {rej['adopted_tool']}={rej['adopted']!r}", flush=True)
            time.sleep(0.4)

    outcomes = Counter(r.outcome for r in records)
    print("== レコード:", dict(outcomes))
    review = [r for r in records if r.outcome == inv_mod.RECORD_NEEDS_REVIEW]
    if review:
        print(f"⚠️  人が見るもの {len(review)} 件（Notion が kintone の更新より後に編集されている。流していない。kintone と Notion の値を見比べて手で直す）")
        for r in review:
            props = ", ".join(f.notion_property for f in r.with_status(inv_mod.FIELD_AMBIGUOUS))
            print(f"      {r.db_key} #{r.kintone_id} {r.label!r}: {props}")
    skipped = [r for r in records if r.outcome == OUTCOME_LIMIT_SKIPPED]
    if skipped:
        print(f"ℹ️  --limit のため今回は流していない候補 {len(skipped)} 件（--limit を外して再実行すると分類し直して流す）")
    report = {
        "mode": "apply" if args.apply else "dry-run", "since": args.since.isoformat(), "until": args.until.isoformat(),
        "records": [{**{k: v for k, v in r.__dict__.items() if k != "fields"}, "fields": [f.__dict__ for f in r.fields]} for r in records],
        "summary": dict(outcomes),
    }
    out_dir = Path(args.report_dir) if args.report_dir else Path.home() / ".local" / "state" / f"crm-sfa-kintone-replay-{datetime.now().strftime('%Y%m%d')}"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{'apply' if args.apply else 'dry-run'}-{datetime.now().strftime('%H%M%S')}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(f"結果 JSON: {out}")
    code = exit_code_for(records, apply=args.apply)
    if code:
        print(f"⚠️  終了コード {code}（1=error あり / 3=流せなかった件あり）。結果 JSON の outcome で対象を確認すること")
    return code


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("src.sync_engine.clients.notion_client").setLevel(logging.ERROR)
    raise SystemExit(run(parse_args()))
