#!/usr/bin/env python3
"""kintone で作られたのに Notion に無い（対応表に無い）レコードを Notion へ作る回収スクリプト（既定 dry-run）。

`backfill_zoho_missed_records.py` の kintone 版。**対象は「作成漏れ」だけ**（更新漏れは
`replay_kintone_missed_updates.py`）。背景は 2026-09-16〜21 の Vercel 停止中に kintone で作られた取引先
（kintone Webhook は再送しない）。

■ やること
    kintone の一覧 API（更新日時が --since 以降）─▶ 対応表に無いもの ＝ 作成漏れ
      ─▶ dry-run: 本番の変換（build_notion_properties_for_new_record）で「必須項目が揃うか」を予測（読み取りだけ）
      ─▶ --apply: 本番と同じ Dispatcher に「新規レコードの通知」として 1 件ずつ流す
          （Notion ページ作成＋対応表登録＋シート行作成。作れるかは Dispatcher が決める）

■ 安全側の設計（Zoho 版と同じ）
- 対応表は本番の Notion 実装を強制。各件の直前に対応表を引き直し、載っていれば流さない（mapped_meanwhile）
- Slack 通知は切る。1 件の失敗で止めない。終了コード 0=全件作成 / 1=error あり / 3=Dispatcher がスキップ

■ 使い方
    .venv/bin/python scripts/backfill_kintone_missed_records.py --since 2026-08-31T00:00+09:00 --until 2026-09-27T05:57+09:00
    ... --apply --limit 5
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

import backfill_zoho_missed_records as backfill  # noqa: E402
import inventory_kintone_missed_updates as inv_mod  # noqa: E402

logger = logging.getLogger("backfill_kintone_missed_records")

PREDICT_CREATE = "create"
PREDICT_MISSING_REQUIRED = "missing_required"
PREDICT_ERROR = "error"


@dataclass
class Missed:
    db_key: str
    kintone_id: str
    label: str = ""
    created_at: str = ""
    predicted: str = ""
    missing_required: list[str] = field(default_factory=list)
    dispatch_reason: str | None = None
    notion_key: str | None = None
    error: str | None = None


def predict_creation(item: Missed, raw: dict[str, Any], store) -> None:
    """本番の変換を通し、必須項目が揃うか予測する（読み取りだけ）。--apply では使わず Dispatcher に任せる。"""
    from src.db_schema.base import Tool
    from src.sync_engine.clients.kintone_client import unwrap_kintone_record
    from src.sync_engine.new_record_builder import build_notion_properties_for_new_record

    try:
        props = build_notion_properties_for_new_record(
            source_tool=Tool.KINTONE, db_key=item.db_key, external_id=item.kintone_id,
            raw_record=unwrap_kintone_record(raw), id_mapping_store=store,
        )
        item.missing_required = backfill.missing_required_properties(item.db_key, props)
        item.predicted = PREDICT_MISSING_REQUIRED if item.missing_required else PREDICT_CREATE
    except Exception as exc:
        item.predicted, item.error = PREDICT_ERROR, repr(exc)[:300]


def exit_code_for(counts: dict[str, int]) -> int:
    if counts.get("error"):
        return 1
    return 3 if any(r not in ("created", "mapped_meanwhile") for r in counts) else 0


def apply_all(dispatcher, items: Sequence[Missed], *, store, sleep_seconds: float = 0.4) -> Counter:
    from src.audit_log.actor_context import set_actor
    from src.db_schema.base import Tool
    from src.sync_engine.sync_event import SyncEvent

    now = datetime.now(timezone.utc)
    for i, it in enumerate(items, 1):
        try:
            already = store.find_by_external_id(Tool.KINTONE, it.kintone_id, db_key=it.db_key)
        except Exception as exc:
            already = None
            logger.warning("対応表の引き直しに失敗（Dispatcher 側の再確認に任せる）: %r", exc)
        if already is not None:
            it.dispatch_reason, it.notion_key = "mapped_meanwhile", already.notion_key
            print(f"  [{i}/{len(items)}] {it.db_key} #{it.kintone_id} → mapped_meanwhile", flush=True)
            continue
        event = SyncEvent(source_tool=Tool.KINTONE, db_key=it.db_key, external_id=it.kintone_id, occurred_at=now, properties={})
        try:
            with set_actor("kintone_backfill"):
                result = dispatcher.dispatch(event)
            it.dispatch_reason = result.reason if result.skipped else "created"
        except Exception as exc:
            it.dispatch_reason, it.error = "error", repr(exc)[:300]
            logger.exception("dispatch failed for kintone_id=%s", it.kintone_id)
        print(f"  [{i}/{len(items)}] {it.db_key} #{it.kintone_id} {it.label!r} → {it.dispatch_reason}", flush=True)
        time.sleep(sleep_seconds)
    return Counter(it.dispatch_reason for it in items)


def parse_args(argv=None) -> argparse.Namespace:
    from src.sync_engine.production_wiring import _KINTONE_DB_ENV_SUFFIX

    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--since", required=True, type=backfill.parse_since)
    p.add_argument("--until", required=True, type=backfill.parse_since)
    p.add_argument("--db-key", dest="db_keys", action="append", choices=sorted(_KINTONE_DB_ENV_SUFFIX))
    p.add_argument("--kintone-id", dest="kintone_ids", action="append", type=inv_mod.kintone_id_arg)
    p.add_argument("--limit", type=int, default=None, help="対象にする作成漏れの上限（先頭 N 件）")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--report-dir", default=None)
    return p.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    backfill.load_env(apply=args.apply)
    from src.db_schema.base import Tool
    from src.sync_engine.production_wiring import _KINTONE_DB_ENV_SUFFIX, build_id_mapping_store, build_production_dispatcher
    from src.sync_engine.zoho_watch_channel import build_zoho_client_from_env

    print(f"== {'APPLY（本番へ書き込み）' if args.apply else 'DRY-RUN（読み取りだけ）'} since={args.since.isoformat()} until={args.until.isoformat()} ==")
    store = build_id_mapping_store()
    if type(store).__name__ != "NotionIdMappingStore":
        raise SystemExit(f"エラー: 対応表が本番と違う実装です（{type(store).__name__}）")

    missed: list[tuple[Missed, dict[str, Any]]] = []
    for db_key in args.db_keys or sorted(_KINTONE_DB_ENV_SUFFIX):
        if inv_mod.kintone_app_and_token(db_key) is None:
            print(f"  {db_key}: kintone の環境変数が無いので飛ばす")
            continue
        raws = inv_mod.fetch_updated_records(db_key, args.since, args.until, args.kintone_ids)
        new = []
        for raw in raws:
            kid = raw["$id"]["value"]
            if store.find_by_external_id(Tool.KINTONE, kid, db_key=db_key) is None:
                new.append((Missed(db_key=db_key, kintone_id=kid, label=inv_mod.label_of(raw), created_at=raw["作成日時"]["value"]), raw))
            time.sleep(0.2)
        print(f"  {db_key}: 期間内に更新 {len(raws)} 件 / 対応表に無い（作成漏れ）{len(new)} 件")
        missed.extend(new)
    if args.limit is not None:
        missed = missed[: args.limit]
    items = [m for m, _ in missed]

    if args.apply:
        zoho = build_zoho_client_from_env()
        dispatcher = build_production_dispatcher(id_mapping_store=store, zoho_client=zoho, slack_notifier=None)
        print(f"== {len(items)} 件を Dispatcher に流す ==")
        counts = apply_all(dispatcher, items, store=store)
        print("== 結果:", dict(counts))
        code = exit_code_for(counts)
    else:
        for m, raw in missed:
            predict_creation(m, raw, store)
            print(f"  {m.db_key} #{m.kintone_id} {m.label!r}（作成 {inv_mod.fmt_jst(m.created_at)}）→ {m.predicted} {m.missing_required or ''}")
        counts = Counter(m.predicted for m in items)
        print("== 予測:", dict(counts))
        code = 1 if counts.get(PREDICT_ERROR) else 0

    out_dir = Path(args.report_dir) if args.report_dir else Path.home() / ".local" / "state" / f"crm-sfa-kintone-backfill-{datetime.now().strftime('%Y%m%d')}"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{'apply' if args.apply else 'dry-run'}-{datetime.now().strftime('%H%M%S')}.json"
    out.write_text(json.dumps({"mode": "apply" if args.apply else "dry-run", "items": [asdict(i) for i in items]}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"結果 JSON: {out}")
    if code:
        print(f"⚠️  終了コード {code}（1=error あり / 3=Dispatcher がスキップした件あり）。結果 JSON で対象を確認すること")
    if not args.apply:
        print("次は --apply（まず --limit 5 で試す）。必須項目不足の件は作られずスキップされる")
    return code


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("src.sync_engine.clients.notion_client").setLevel(logging.ERROR)
    raise SystemExit(run(parse_args()))
