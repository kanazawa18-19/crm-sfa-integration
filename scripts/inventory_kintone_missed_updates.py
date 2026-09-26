#!/usr/bin/env python3
"""kintone 側で直したのに Notion に届いていない更新の洗い出し（読み取りだけ、2026-09-27）。

■ 何のために

2026-08-31（`31cf84c`）〜09-27 05:57 JST（`c61d728` 配備）の間、kintone / Zoho 発の**既存レコードの更新**は
Notion に書く直前に TypeError で落ちていた（`docs/zoho_missed_updates_replay_note.md`）。
Zoho 側は Timeline API で「どの項目がいつ変わったか」が取れるので `replay_zoho_missed_updates.py` で扱えるが、
kintone には項目単位の変更履歴 API が無い。そこでこのスクリプトは

    kintone の一覧 API（更新日時が期間内）─▶ 対応表で Notion ページを引く ─▶ 同期対象の項目だけを
    本番と同じ変換（kintone_payload_to_sync_event）に通す ─▶ Notion の現在値と比べる

で「今ズレている項目」を一覧にする。**履歴が無いので、期間より前からズレていた項目も混ざる**
（区別できない。表の kintone の更新時刻・更新者と Notion の最終更新を見て人が判断する）。

■ 分類（項目ごと）

    already_synced   Notion が既に同じ値
    whitespace_only  前後の空白だけが違う（実質同じ。Notion 側の表記ゆれ。流さない）
    safe             違う。Notion ページの最終更新が kintone の更新より前 → kintone の値を流してよい見込み
    ambiguous        違う。Notion ページが kintone の更新より後に編集されている → 人が見る

■ 分類（レコードごと）

    not_mapped      対応表に無い（作成漏れ。ここでは扱わない。件数と一覧だけ出す）
    ready           safe な項目が 1 つ以上ある＝流す候補
    needs_review    safe は無く ambiguous だけ＝人が見る
    nothing_to_do   同期対象の項目に違いが無い
    error           読み取りに失敗（結果 JSON の error に理由）

  期間内に作られたレコードも全部比べる（`created_in_window` 印を付ける）。作成そのものは別経路で
  Notion に届いているが、作成直後の編集は同じ不具合で落ちているかもしれないので、作成時刻で除外しない
  （shirokuma-sec レビュー BLOCKER。実測でも作成から 60 秒以内に編集された件が 2 件あった）。

■ 書き込みは一切しない。反映のしかたは結果を見て決める。この結果 JSON をもとに後日 Notion へ流す
  仕組みを作るなら、`replay_zoho_missed_updates.py` と同じく**書く直前に kintone と Notion を読み直して
  分類時から動いていないか確かめる**こと（結果 JSON の値は分類時点のもの）。

■ 使い方

    .venv/bin/python scripts/inventory_kintone_missed_updates.py \\
        --since 2026-08-31T00:00+09:00 --until 2026-09-27T05:57+09:00
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

# `load_env`（config/.env を読み、対応表を本番の Notion に固定し、dry-run では名寄せを切る）と
# `parse_since` は Zoho の回収スクリプトのものをそのまま使う（kintone 専用ではないが同じ前提でよい）
import backfill_zoho_missed_records as backfill  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("inventory_kintone")

JST = timezone(timedelta(hours=9))

# 項目の分類
FIELD_ALREADY_SYNCED = "already_synced"
FIELD_WHITESPACE_ONLY = "whitespace_only"
FIELD_SAFE = "safe"
FIELD_AMBIGUOUS = "ambiguous"

# レコードの分類
RECORD_NOT_MAPPED = "not_mapped"
RECORD_READY = "ready"
RECORD_NEEDS_REVIEW = "needs_review"
RECORD_NOTHING_TO_DO = "nothing_to_do"
RECORD_ERROR = "error"


@dataclass
class FieldDiff:
    notion_property: str
    kintone_value: Any
    notion_value: Any
    status: str


@dataclass
class RecordInventory:
    db_key: str
    kintone_id: str
    label: str = ""
    updater: str = ""
    created_at: str = ""
    updated_at: str = ""
    created_in_window: bool = False  # 期間内に作られたレコード（作成は別経路で届いているが、その後の編集は比べる）
    notion_key: str | None = None
    notion_last_edited_at: str | None = None
    outcome: str = ""
    error: str | None = None
    fields: list[FieldDiff] = field(default_factory=list)

    def with_status(self, status: str) -> list[FieldDiff]:
        return [f for f in self.fields if f.status == status]


# ---------------------------------------------------------------------------------------------
# 純粋な部分（I/O なし。テストはここを直接叩く）
# ---------------------------------------------------------------------------------------------


def floor_to_minute(value: datetime) -> datetime:
    return value.replace(second=0, microsecond=0)


def parse_kintone_time(value: str) -> datetime:
    """kintone の日時は UTC の "Z" 表記（例 2026-09-15T00:25:00Z）。"""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def kintone_query_time(value: datetime) -> str:
    """kintone のクエリに渡す形（+0000 の UTC）。"""
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+0000")


def kintone_id_arg(value: str) -> str:
    """--kintone-id はクエリ文字列に埋め込むので数字だけを通す。"""
    if not value.isdigit():
        raise argparse.ArgumentTypeError(f"kintone のレコード番号は数字です: {value!r}")
    return value


def classify_field(kintone_value: Any, notion_value: Any, *, notion_last_edited_at: datetime | None, kintone_updated_at: datetime) -> str:
    """項目 1 つを分類する。

    - 同じ値なら already_synced。前後の空白だけの違いは whitespace_only（Notion 側の表記ゆれ。流さない）
    - 違うとき、Notion ページの最終更新が kintone の更新より前なら safe。Notion の `last_edited_time` は
      分単位に丸められるので、kintone の更新と同じ分に Notion が触られていたら安全と言い切れない（ambiguous）。
      最終更新時刻が取れないときも ambiguous（安全側に倒す）
    """
    from src.sync_engine.conflict_resolver import _is_empty, _values_equal

    if _values_equal(kintone_value, notion_value) or (_is_empty(kintone_value) and _is_empty(notion_value)):
        return FIELD_ALREADY_SYNCED
    if isinstance(kintone_value, str) and isinstance(notion_value, str) and kintone_value.strip() == notion_value.strip():
        return FIELD_WHITESPACE_ONLY
    notion_untouched = notion_last_edited_at is not None and notion_last_edited_at < floor_to_minute(kintone_updated_at)
    return FIELD_SAFE if notion_untouched else FIELD_AMBIGUOUS


def record_outcome(inv: RecordInventory) -> str:
    if inv.error:
        return RECORD_ERROR
    if inv.outcome == RECORD_NOT_MAPPED:
        return inv.outcome
    if inv.with_status(FIELD_SAFE):
        return RECORD_READY
    if inv.with_status(FIELD_AMBIGUOUS):
        return RECORD_NEEDS_REVIEW
    return RECORD_NOTHING_TO_DO


def label_of(raw: dict[str, Any]) -> str:
    for code in ("顧客名", "案件名", "件名", "タイトル"):
        value = raw.get(code)
        if isinstance(value, dict) and value.get("value"):
            return str(value["value"])
    return ""


def fmt_jst(value: str | None) -> str:
    if not value:
        return "-"
    try:
        return datetime.fromisoformat(value).astimezone(JST).strftime("%m-%d %H:%M")
    except ValueError:
        return value


# ---------------------------------------------------------------------------------------------
# I/O（kintone 一覧・対応表・Notion ページ。どれも読み取りだけ）
# ---------------------------------------------------------------------------------------------


def kintone_app_and_token(db_key: str) -> tuple[str, str] | None:
    import os

    from src.sync_engine.production_wiring import _KINTONE_DB_ENV_SUFFIX

    suffix = _KINTONE_DB_ENV_SUFFIX[db_key]
    app = os.environ.get(f"KINTONE_APP_ID_{suffix}")
    token = os.environ.get(f"KINTONE_API_TOKEN_{suffix}")
    return (app, token) if app and token else None


def fetch_updated_records(db_key: str, since: datetime, until: datetime, only_ids: list[str] | None) -> list[dict[str, Any]]:
    """更新日時が期間内のレコードを全項目付きで取る（$id 昇順に 500 件ずつ、429 / 5xx は本番と同じ再試行）。"""
    import os

    from src.sync_engine.clients._http import request_with_retry

    domain = os.environ["KINTONE_DOMAIN"]
    url = (domain if domain.startswith("http") else f"https://{domain}") + "/k/v1/records.json"
    app, token = kintone_app_and_token(db_key)  # type: ignore[misc]  (呼び出し元で None を弾いている)
    records: list[dict[str, Any]] = []
    last_id = 0
    while True:
        conds = [f'更新日時 >= "{kintone_query_time(since)}"', f'更新日時 <= "{kintone_query_time(until)}"', f"$id > {last_id}"]
        if only_ids:
            conds.append("$id in (" + ",".join(only_ids) + ")")
        query = " and ".join(conds) + " order by $id asc limit 500"
        resp = request_with_retry("GET", url, headers={"X-Cybozu-API-Token": token}, params={"app": app, "query": query}, timeout=60)
        resp.raise_for_status()
        page = resp.json()["records"]
        if not page:
            break
        records.extend(page)
        last_id = int(page[-1]["$id"]["value"])
    return records


def plan_record(inv: RecordInventory, raw: dict[str, Any], *, app_id: str, since: datetime, store, notion_client) -> None:
    """1 レコード分の読み取りと分類（書き込みはしない）。失敗は inv.error に残して呼び出し元へ返す。"""
    from src.db_schema.base import Tool
    from src.sync_engine.clients._notion_keys import NOTION_LAST_EDITED_TIME_KEY
    from src.sync_engine.webhook_handlers.kintone_webhook import kintone_payload_to_sync_event

    try:
        inv.label = label_of(raw)
        created = parse_kintone_time(raw["作成日時"]["value"])
        updated = parse_kintone_time(raw["更新日時"]["value"])
        inv.created_at, inv.updated_at = created.isoformat(), updated.isoformat()
        inv.updater = (raw.get("更新者", {}).get("value") or {}).get("name", "")
        inv.created_in_window = created >= since
        # 対応表はレコードごとに引く（1 件 1 回の Notion 検索）。db_key 単位の一括取得（list_by_db）は
        # 取引先マスターだけで数万件あり、数百件のためには重すぎる
        mapping = store.find_by_external_id(Tool.KINTONE, inv.kintone_id, db_key=inv.db_key)
        if mapping is None:
            inv.outcome = RECORD_NOT_MAPPED
            return
        inv.notion_key = mapping.notion_key

        # 本番の Webhook と同じ変換に通す。対応表と Notion クライアントも渡して、取引先マスターの
        # リレーションの「後勝ち」上書き防止ガードまで本番と同じにする（どちらも読み取りだけ）
        event = kintone_payload_to_sync_event(
            {"app": {"id": app_id}, "record": raw}, {},
            app_id_to_db_key={app_id: inv.db_key}, id_mapping_store=store, notion_client=notion_client,
        )
        page = notion_client.get_page(mapping.notion_key)
        if page is None:
            inv.error = "notion_page_not_found"
            return
        last_edited = page.get(NOTION_LAST_EDITED_TIME_KEY)
        last_edited = last_edited if isinstance(last_edited, datetime) else None
        inv.notion_last_edited_at = last_edited.isoformat() if last_edited else None
        for prop, value in sorted(event.properties.items()):
            status = classify_field(value, page.get(prop), notion_last_edited_at=last_edited, kintone_updated_at=updated)
            inv.fields.append(FieldDiff(prop, value, page.get(prop), status))
    except Exception as exc:  # 1 件の失敗で全体を止めない
        inv.error = repr(exc)[:300]
        logger.exception("plan failed for %s %s", inv.db_key, inv.kintone_id)
    finally:
        inv.outcome = record_outcome(inv)


def print_record(i: int, total: int, inv: RecordInventory) -> None:
    mark = "（期間内に作成）" if inv.created_in_window else ""
    head = f"[{i}/{total}] {inv.db_key} #{inv.kintone_id} {inv.label!r}{mark} → {inv.outcome}"
    if inv.outcome in (RECORD_NOT_MAPPED, RECORD_NOTHING_TO_DO):
        print(head)
        return
    print(f"{head}  kintone更新 {fmt_jst(inv.updated_at)} by {inv.updater or '?'} / Notion最終更新 {fmt_jst(inv.notion_last_edited_at)}")
    if inv.error:
        print(f"      error: {inv.error}")
    for f in inv.fields:
        if f.status == FIELD_ALREADY_SYNCED:
            continue
        print(f"      {f.status:15} {f.notion_property}: kintone={f.kintone_value!r}  Notion現在値={f.notion_value!r}")


def parse_args(argv=None) -> argparse.Namespace:
    from src.sync_engine.production_wiring import _KINTONE_DB_ENV_SUFFIX

    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--since", required=True, type=backfill.parse_since, help="この時刻以降に更新されたレコード（ISO 8601、タイムゾーン必須）")
    p.add_argument("--until", required=True, type=backfill.parse_since, help="この時刻までに更新されたレコード")
    p.add_argument("--db-key", dest="db_keys", action="append", choices=sorted(_KINTONE_DB_ENV_SUFFIX), help="対象 DB（複数可。省略時は kintone と同期している 3 つ全部）")
    p.add_argument("--kintone-id", dest="kintone_ids", action="append", type=kintone_id_arg,
                   help="このレコード番号だけを見る（複数可）。番号はアプリごとに独立なので --db-key と併用する")
    p.add_argument("--limit", type=int, default=None, help="先頭 N 件だけ扱う")
    p.add_argument("--report-dir", default=None, help="結果 JSON の保存先（既定 ~/.local/state/crm-sfa-kintone-inventory-YYYYMMDD）")
    return p.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    backfill.load_env(apply=False)  # 対応表は本番（Notion）、名寄せ（確認待ちキューへの書き込み）は切る
    from src.sync_engine.production_wiring import _KINTONE_DB_ENV_SUFFIX, build_id_mapping_store, build_notion_clients_by_db

    print(f"== DRY-RUN（読み取りだけ） since={args.since.isoformat()} until={args.until.isoformat()} ==")
    print("   注意: kintone には項目単位の変更履歴が無いので、期間より前からズレていた項目も混ざる。"
          "kintone の更新時刻・更新者と Notion の最終更新を見て判断すること")
    store = build_id_mapping_store()
    if type(store).__name__ != "NotionIdMappingStore":
        raise SystemExit(f"エラー: 対応表が本番と違う実装です（{type(store).__name__}）")
    notion_clients = build_notion_clients_by_db()
    if not notion_clients:
        raise SystemExit("エラー: Notion クライアントを作れません（NOTION_API_KEY / DB ID）")

    db_keys = args.db_keys or sorted(_KINTONE_DB_ENV_SUFFIX)
    items: list[tuple[RecordInventory, dict[str, Any], str]] = []
    fetch_errors: dict[str, str] = {}
    for db_key in db_keys:
        creds = kintone_app_and_token(db_key)
        if creds is None:
            print(f"  {db_key}: kintone の環境変数が無いので飛ばす")
            continue
        if db_key not in notion_clients:
            raise SystemExit(f"エラー: {db_key} 用の Notion クライアントがありません（DB ID 未設定）")
        try:
            raws = fetch_updated_records(db_key, args.since, args.until, args.kintone_ids)
        except Exception as exc:  # 1 アプリの失敗で他のアプリまで止めない
            fetch_errors[db_key] = repr(exc)[:300]
            print(f"  {db_key}: kintone の一覧取得に失敗 → 飛ばす（{fetch_errors[db_key]}）")
            continue
        print(f"  {db_key}: 期間内に更新されたレコード {len(raws)} 件")
        for raw in raws:
            items.append((RecordInventory(db_key=db_key, kintone_id=raw["$id"]["value"]), raw, creds[0]))
    if args.limit is not None:
        items = items[: args.limit]

    print(f"== {len(items)} 件を対応表・Notion と突き合わせ ==")
    print("   凡例: safe=kintone の値を流してよい見込み / ambiguous=Notion が後から編集されている（人が見る） /"
          " whitespace_only=空白だけの違い（流さない） / not_mapped=対応表に無い（作成漏れ、ここでは扱わない）")
    for i, (inv, raw, app_id) in enumerate(items, 1):
        plan_record(inv, raw, app_id=app_id, since=args.since, store=store, notion_client=notion_clients[inv.db_key])
        print_record(i, len(items), inv)
        time.sleep(0.2)

    records = [inv for inv, _, _ in items]
    outcomes = Counter(r.outcome for r in records)
    field_status = Counter(f.status for r in records for f in r.fields)
    print("== レコード:", dict(outcomes))
    print("== 項目:", dict(field_status))
    for outcome, title in ((RECORD_READY, "流す候補（safe あり）"), (RECORD_NEEDS_REVIEW, "人が見るもの（Notion が後から編集されている）"), (RECORD_NOT_MAPPED, "対応表に無い（作成漏れ）")):
        hits = [r for r in records if r.outcome == outcome]
        if hits:
            print(f"-- {title} {len(hits)} 件")
            for r in hits:
                props = ", ".join(f.notion_property for f in r.fields if f.status in (FIELD_SAFE, FIELD_AMBIGUOUS))
                print(f"      {r.db_key} #{r.kintone_id} {r.label!r}" + (f": {props}" if props else ""))

    report = {
        "mode": "dry-run",
        "since": args.since.isoformat(),
        "until": args.until.isoformat(),
        "fetch_errors": fetch_errors,
        "records": [{**{k: v for k, v in r.__dict__.items() if k != "fields"}, "fields": [f.__dict__ for f in r.fields]} for r in records],
        "summary": {"records": dict(outcomes), "fields": dict(field_status)},
    }
    out_dir = Path(args.report_dir) if args.report_dir else Path.home() / ".local" / "state" / f"crm-sfa-kintone-inventory-{datetime.now().strftime('%Y%m%d')}"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"dry-run-{datetime.now().strftime('%H%M%S')}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(f"結果 JSON: {out}")
    return 1 if outcomes.get(RECORD_ERROR) or fetch_errors else 0


if __name__ == "__main__":
    sys.exit(run(parse_args()))
