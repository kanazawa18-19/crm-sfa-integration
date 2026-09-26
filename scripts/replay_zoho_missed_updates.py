#!/usr/bin/env python3
"""Zoho の通知（watch）が止まっていた間に Zoho 側で**更新**された既存レコード（対応表にあるもの）の
変更を、Notion へ届け直すスクリプト。`backfill_zoho_missed_records.py` が「作成漏れ」を扱うのに対し、
こちらは「更新漏れ」を扱う。

■ 何が難しいか（2026-09-27）
通常の Zoho 通知は「変わった項目だけ」（affected_values）を運ぶので、Dispatcher は項目ごとに
Notion の現在値と突き合わせて書く。止まっていた間の変更にはこの差分が無い。レコード全体を
流すと、Zoho と Notion で違う値を持つ全項目が「競合」として最新更新優先で解決され、
Notion 側で後から直した値や、もともと同期対象外だった違いまで巻き込んで上書きしうる。

■ どう解くか
Zoho の Timeline API（`GET /crm/v6/{module}/{id}/__timeline`。v3 では API_NOT_SUPPORTED、v5 以降）が
「どの項目が・いつ・何から何へ」変わったかを返すので、これで差分を組み立て直す。
値の形は表示用（例 "￥ 20,000"）で通知と違うため、**変わった項目名と時刻だけ** Timeline から取り、
値は Zoho の現在値（v2 GET、通知と同じ形）を使う。

    Zoho v6 一覧（If-Modified-Since）で --since 以降に変更された ID
      ──▶ 対応表にあるもの（無いものは backfill_zoho_missed_records.py の担当）
      ──▶ 1 件ずつ Timeline で「--since 以降に変わった項目」と各項目の最終変更時刻を取る
      ──▶ 項目ごとに Zoho の現在値を本番と同じ変換（zoho_payload_to_sync_events）に通し、
          Notion の現在値・ページ最終更新時刻と比べて分類する
            already_synced  Notion が既に同じ値（通知復旧後の同期や Notion 発の変更で反映済み）
            safe            Notion ページが Zoho の変更より後に触られていない → Zoho の値で更新してよい
            ambiguous       Notion ページが Zoho の変更より後に編集されている → **触らない**。人が見る
            unmapped        同期対象外の項目（変換表に無い）→ 触らない
            relation_pending 読み取りだけの変換では値が決まらない項目（取引先・連絡先・商品などの名寄せ）。
                            Notion ページが Zoho の変更より後に触られていなければ、--apply で本番と同じ解決を試みる
            not_converted   変換で落ちた項目で、Notion ページが後から触られているもの → 触らない
            missing_in_record  Zoho の現在値に項目が無い（サブフォーム等）→ 触らない
            changed_after_until  --until より後にも変わった項目。現在値は期間内の値ではないので触らない
      ──▶ --apply: safe（と relation_pending）の項目だけを「Zoho の通知」の形に組み立て直し、
          本番と同じ変換 → Dispatcher。書く直前に Notion ページの最終更新時刻を読み直し、
          分類したときから動いていたら流さない（notion_edited_after_plan）

■ Notion 側の編集を潰さない根拠
- 流すのは Zoho で実際に変わった項目だけ。Notion だけで直した項目には触らない
- その項目についても、Notion ページの最終更新時刻（`last_edited_time`）が Zoho の変更より後なら
  流さない（ambiguous）。Notion の `last_edited_time` は分単位に丸められるので、同じ分は安全側に倒す
- Notion 側で先に変えて Zoho 側で後から変えた項目は、本番と同じ「最新更新優先」で Zoho が勝つ

■ イベントの時刻を「今」にする理由
Dispatcher は対応表の最終同期時刻より古いイベントを `stale_event` として捨てる（古い通知の巻き戻り
防止）。通知復旧後に別項目が同期されたレコードでは、止まっていた間の変更時刻はこの下限より古く、
そのまま流すと捨てられる。止まっていた間の変更は「捨ててよい古い通知」ではなく「まだ届いていない
通知」なので、イベント時刻は再送時点（今）にする。safe な項目は Notion 側にそれより新しい編集が
無いことを確かめてあるので、時刻を今にしても結果は変わらない（Zoho の値が採用される）。
副作用として、そのレコードの最終同期時刻が今に進み、再送時点より前に起きた通知が遅れて届いた場合は
捨てられる（本番の通知は数秒で届くので実害は無い）。

■ 使い方（既定は dry-run。本番 Notion への書き込みは --apply のときだけ）
    .venv/bin/python scripts/replay_zoho_missed_updates.py --since 2026-09-15T16:00+09:00
    .venv/bin/python scripts/replay_zoho_missed_updates.py --since ... --module Deals
    .venv/bin/python scripts/replay_zoho_missed_updates.py --since ... --apply [--limit 5]
    .venv/bin/python scripts/replay_zoho_missed_updates.py --since ... --apply --trust-zoho-id <ID>
        （人が見て「Zoho の値でよい」と決めたレコードは、ambiguous も safe として流す）
    --until は「Timeline のどの変更まで見るか」の絞り込み。対象レコードの収集は --since 以降の全部

■ 環境変数（`config/.env`）は backfill_zoho_missed_records.py と同じ。ただし新規ページは作らない
  （AUTO_CREATE_NEW_RECORDS_ENABLED は常に false。対応表から消えたレコードは unknown_record で止まる）

■ 安全側の設計
- 対応表は本番と同じ Notion 実装を強制する（backfill と同じ）
- 分類は dry-run でも --apply でも Zoho・Notion の読み取りだけ。名寄せ（RELATION_SYNC_ENABLED）は分類の間
  必ず切るので、書かないレコードの分まで確認待ちキュー（RelationReviewQueue）に書き込むことは無い。
  名寄せを試みるのは --apply で実際に流すレコードだけ（本番の通知と同じ副作用）
- --apply でも ambiguous は流さない。流すのは --trust-zoho-id で個別に指定したレコードだけ
- Zoho の現在値に項目が無ければ流さない（無いことを「空欄にした」と誤解しない）
- 1 件の失敗で止めない。終了コードは結果で変える（error があれば 1、Dispatcher がスキップすれば 3）
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

import backfill_zoho_missed_records as backfill  # noqa: E402  作成漏れ回収と同じ部品を使う

logger = logging.getLogger("replay_zoho_missed_updates")

REPLAY_ACTOR = "zoho_replay"
TIMELINE_PAGE_SIZE = 200
TIMELINE_ACTIONS_WITH_FIELDS = ("updated",)  # 項目の変更履歴を持つのは updated だけ（added は全項目が新規）

# 項目の分類
FIELD_ALREADY_SYNCED = "already_synced"
FIELD_SAFE = "safe"
FIELD_AMBIGUOUS = "ambiguous"
FIELD_UNMAPPED = "unmapped"
FIELD_RELATION_PENDING = "relation_pending"
FIELD_NOT_CONVERTED = "not_converted"
FIELD_MISSING_IN_RECORD = "missing_in_record"
FIELD_CHANGED_AFTER_UNTIL = "changed_after_until"  # --until より後にも変わっている（現在値は期間内の値ではない）

# レコードの結果
RECORD_NOTHING_TO_DO = "nothing_to_do"  # safe が 0（全部 already_synced / 対象外）
RECORD_NEEDS_REVIEW = "needs_review"  # safe が 0 で ambiguous がある
RECORD_READY = "ready"  # dry-run で safe がある（--apply なら流す）
RECORD_ERROR = "error"


# ---------------------------------------------------------------------------------------------
# データ
# ---------------------------------------------------------------------------------------------


@dataclass
class FieldChange:
    """Zoho で変わった項目 1 つ分。"""

    api_name: str
    audited_at: datetime  # Timeline 上の最終変更時刻（--since 以降）
    display_old: str | None = None  # Timeline の表示値（人が読む用。値の比較には使わない）
    display_new: str | None = None
    order_uncertain: bool = False  # 同じ時刻に同じ項目が複数回変わっていて、old/new の並びを決められない
    label: str | None = None
    notion_property: str | None = None
    zoho_value: Any = None  # Zoho の現在値（v2 GET、通知と同じ形）
    converted_value: Any = None  # 本番の変換を通した後の値（Notion に書く形）
    notion_value: Any = None
    status: str = ""
    drifted_before: bool = False  # Zoho の変更前の値と Notion の現在値が既に違っていた（以前からのズレ）


@dataclass
class RecordReplay:
    """対応表にある Zoho レコード 1 件の再送計画と結果。"""

    zoho_id: str
    module: str
    db_key: str
    notion_key: str
    label: str = ""
    zoho_last_activity_at: str | None = None
    notion_last_edited_at: str | None = None
    planned_at: str | None = None  # 分類した時刻。書く直前の再確認で「それ以降の Zoho 変更」を見る
    fields: list[FieldChange] = field(default_factory=list)
    forced: bool = False  # --trust-zoho-id で ambiguous も流す指定
    outcome: str = ""  # RECORD_* または --apply 後の Dispatcher の結果
    written: dict[str, list[str]] = field(default_factory=dict)  # --apply: プロパティ → 書き込んだツール
    rejected: list[dict[str, Any]] = field(default_factory=list)  # --apply: 競合で捨てた値（Dispatcher の判定）
    error: str | None = None

    def fields_with(self, status: str) -> list[FieldChange]:
        return [f for f in self.fields if f.status == status]

    def replayable(self) -> list[FieldChange]:
        statuses = {FIELD_SAFE, FIELD_RELATION_PENDING}
        if self.forced:
            statuses.add(FIELD_AMBIGUOUS)
        return [f for f in self.fields if f.status in statuses]


# ---------------------------------------------------------------------------------------------
# 純粋な判定（テスト対象）
# ---------------------------------------------------------------------------------------------


def parse_zoho_time(value: str) -> datetime:
    """Timeline の audited_time（例 "2026-09-24T17:32:07+09:00"）を aware datetime にする。"""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f"Zoho の時刻にタイムゾーンがありません: {value!r}")
    return parsed


def changed_fields_from_timeline(
    entries: Iterable[Mapping[str, Any]], since: datetime, until: datetime | None = None
) -> dict[str, FieldChange]:
    """Timeline のエントリ列から、[since, until] の間に変わった項目を api_name ごとにまとめる。
    同じ項目が何度も変わっていれば、最終変更時刻と最後の表示値、最初の変更前の表示値を残す。
    `updated` 以外（added / slack_notification 等）は項目の履歴を持たないので見ない。"""
    found: dict[str, FieldChange] = {}
    dated: list[tuple[datetime, Mapping[str, Any]]] = []
    for entry in entries:
        if entry.get("action") not in TIMELINE_ACTIONS_WITH_FIELDS:
            continue
        audited_raw = entry.get("audited_time")
        if not audited_raw:
            continue
        dated.append((parse_zoho_time(str(audited_raw)), entry))
    # API は新しい順に返すが、それに依存せず自分で並べる（同時刻・ページ境界で old/new を取り違えない）
    dated.sort(key=lambda item: item[0], reverse=True)
    for audited_at, entry in dated:
        if audited_at < since or (until is not None and audited_at > until):
            continue
        for history in entry.get("field_history") or []:
            api_name = history.get("api_name")
            if not api_name:
                continue
            value = history.get("_value") or {}
            current = found.get(api_name)
            if current is None:
                found[api_name] = FieldChange(
                    api_name=str(api_name),
                    audited_at=audited_at,
                    display_old=_display(value.get("old")),
                    display_new=_display(value.get("new")),
                )
                continue
            if audited_at > current.audited_at:
                current.audited_at = audited_at
                current.display_new = _display(value.get("new"))
            elif audited_at == current.audited_at:
                # 同じ秒に同じ項目が複数回。並びは API 任せになるので old/new を推測しない（ChatGPT レビュー WARN）
                current.order_uncertain = True
                current.display_old = None
            else:
                # より古い変更 → 「変更前」はこちらの old
                current.display_old = _display(value.get("old"))
    return found


def fields_changed_after(entries: Iterable[Mapping[str, Any]], until: datetime | None) -> set[str]:
    """`until` より後にも変わった項目の api_name。Zoho の現在値はその後の変更を含むので、これらは
    「期間内の変更」として流せない（ChatGPT レビュー BLOCKER、2026-09-27）。until 無しなら空。"""
    if until is None:
        return set()
    changed: set[str] = set()
    for entry in entries:
        if entry.get("action") not in TIMELINE_ACTIONS_WITH_FIELDS or not entry.get("audited_time"):
            continue
        if parse_zoho_time(str(entry["audited_time"])) > until:
            changed.update(str(h["api_name"]) for h in entry.get("field_history") or [] if h.get("api_name"))
    return changed


def _display(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def floor_to_minute(value: datetime) -> datetime:
    return value.replace(second=0, microsecond=0)


def classify_field(
    change: FieldChange,
    *,
    notion_last_edited_at: datetime | None,
    in_record: bool,
    mapped: bool,
    converted: bool,
    changed_after_until: bool = False,
) -> str:
    """項目 1 つを分類する。順序に意味がある（対象外 → 反映済み → 安全か）。

    - 変換表に無い項目は同期対象外（unmapped）
    - Zoho の現在値に項目が無ければ流さない（missing_in_record）
    - --until より後にも変わっていれば流さない（changed_after_until。現在値は期間内の値ではない）
    - 読み取りだけの変換で値が決まらなかった（名寄せが要るリレーション・上書き防止ガード等）なら、
      Notion が後から触られていない場合だけ --apply で本番の解決を試みる（relation_pending）。
      触られていれば流さない（not_converted）
    - Notion の値が既に同じなら何もしない（already_synced）
    - Notion ページの最終更新が Zoho の変更より前なら安全（safe）。Notion の `last_edited_time` は
      分単位に丸められるので、Zoho の変更と同じ分に Notion が更新されていたら安全と言い切れない
      （ambiguous）。最終更新時刻が取れないときも ambiguous
    """
    from src.sync_engine.conflict_resolver import _is_empty, _values_equal

    if not mapped:
        return FIELD_UNMAPPED
    if not in_record:
        return FIELD_MISSING_IN_RECORD
    if changed_after_until:
        return FIELD_CHANGED_AFTER_UNTIL
    notion_untouched = notion_last_edited_at is not None and notion_last_edited_at < floor_to_minute(change.audited_at)
    if not converted:
        return FIELD_RELATION_PENDING if notion_untouched else FIELD_NOT_CONVERTED
    if _values_equal(change.converted_value, change.notion_value) or (
        _is_empty(change.converted_value) and _is_empty(change.notion_value)
    ):
        return FIELD_ALREADY_SYNCED
    return FIELD_SAFE if notion_untouched else FIELD_AMBIGUOUS


def record_outcome(record: RecordReplay) -> str:
    if record.error:
        return RECORD_ERROR
    if record.replayable():
        return RECORD_READY
    if record.fields_with(FIELD_AMBIGUOUS):
        return RECORD_NEEDS_REVIEW
    return RECORD_NOTHING_TO_DO


def build_notification_payload(
    module: str, zoho_id: str, values: Mapping[str, Any], occurred_at: datetime
) -> dict[str, Any]:
    """Zoho が送ってくる通知（Notifications API）と同じ形に組み立て直す。
    本番の変換（`zoho_payload_to_sync_events`）にそのまま通せる。"""
    return {
        "module": module,
        "ids": [zoho_id],
        "affected_values": [{"record_id": zoho_id, "values": dict(values)}],
        "server_time": int(occurred_at.timestamp() * 1000),
    }


def exit_code_for(records: Sequence[RecordReplay], *, apply: bool) -> int:
    """終了コード。1=error あり（dry-run でも。読めなかったレコードがあるのに「全件分類できた」と見せない）/
    3=--apply で流せなかった件あり / 0=それ以外。needs_review は正常な判定結果なので 0。"""
    if any(r.outcome == RECORD_ERROR for r in records):
        return 1
    if not apply:
        return 0
    if any(r.outcome not in (RECORD_NOTHING_TO_DO, RECORD_NEEDS_REVIEW, "updated") for r in records):
        return 3
    return 0


# ---------------------------------------------------------------------------------------------
# 引数・環境
# ---------------------------------------------------------------------------------------------


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Zoho 通知停止中の更新漏れを Notion へ届け直す（既定は dry-run）")
    p.add_argument("--since", required=True, type=backfill.parse_since, help="この時刻以降の変更（ISO 8601、タイムゾーン必須）")
    p.add_argument("--until", type=backfill.parse_since, default=None, help="この時刻までの変更に絞る（省略時は現在まで）")
    p.add_argument("--module", dest="modules", action="append", help="Zoho モジュール名（複数可。省略時は購読対象の 6 つ）")
    p.add_argument("--zoho-id", dest="zoho_ids", action="append", help="このレコードだけを見る（複数可）")
    p.add_argument("--trust-zoho-id", dest="trust_zoho_ids", action="append", default=[],
                   help="このレコードは ambiguous な項目も Zoho の値で流す（人が見て決めたときだけ。複数可）")
    p.add_argument("--limit", type=int, default=None, help="先頭 N 件だけ扱う")
    p.add_argument("--apply", action="store_true", help="本番 Notion へ書き込む（省略時は読み取りだけ）")
    p.add_argument("--report-dir", default=None, help="結果 JSON の保存先（既定 ~/.local/state/crm-sfa-zoho-replay-YYYYMMDD）")
    return p.parse_args(argv)


def load_env(apply: bool, env_path: Path | None = None) -> None:
    """backfill と同じ読み込み。ただし新規ページは作らない（更新漏れの再送で作成経路に入る理由が無い）。"""
    backfill.load_env(apply, env_path)
    os.environ["AUTO_CREATE_NEW_RECORDS_ENABLED"] = "false"


def zoho_v6_base_url() -> str:
    """Timeline API は v5 以降でしか使えない（v3 は API_NOT_SUPPORTED）。v3 の URL から v6 を組み立てる。"""
    v3 = backfill.zoho_v3_base_url()
    return v3[: -len("/v3")] + "/v6" if v3.endswith("/v3") else v3


# ---------------------------------------------------------------------------------------------
# Zoho / Notion の読み取り
# ---------------------------------------------------------------------------------------------


def fetch_timeline(client, module: str, zoho_id: str, since: datetime) -> list[dict[str, Any]]:
    """`GET /crm/v6/{module}/{id}/__timeline` を page_token で最後までたどる（204 は履歴なし）。
    新しい順に返るが、その並びに頼って途中で止めない（ChatGPT レビュー WARN。1 件でも欠けると
    変更を取りこぼす。1 レコードの履歴は高々数ページ）。`since` は呼び出し元の絞り込み用に受けるだけ。"""
    from src.sync_engine.clients._http import raise_for_error
    from src.sync_engine.clients.zoho_client import ZohoApiError

    base = zoho_v6_base_url()
    entries: list[dict[str, Any]] = []
    page_token: str | None = None
    while True:
        params: dict[str, Any] = {"per_page": TIMELINE_PAGE_SIZE}
        if page_token:
            params["page_token"] = page_token
        response = client.request("GET", f"{base}/{module}/{zoho_id}/__timeline?{urlencode(params)}", idempotent=True)
        if response.status_code in (204, 304):
            break
        raise_for_error(response, ZohoApiError)
        body = response.json()
        page = [e for e in body.get("__timeline", []) if isinstance(e, Mapping)]
        entries.extend(page)
        info = body.get("info") or {}
        page_token = info.get("next_page_token")
        if not info.get("more_records") or not page_token:
            break
        time.sleep(0.2)
    return entries


def resolve_notion_property(module: str, db_key: str, api_name: str) -> tuple[str | None, str | None]:
    """api_name → (Zoho ラベル, Notion プロパティ名)。変換表に無ければ (ラベル or None, None)。"""
    from src.sync_engine.webhook_handlers.zoho_field_transforms import ZOHO_LABEL_FIELD_MAPPINGS
    from src.sync_engine.zoho_field_mapping import resolve_zoho_field_label

    label = resolve_zoho_field_label(module, api_name)
    if label is None:
        return None, None
    mapping = ZOHO_LABEL_FIELD_MAPPINGS.get(db_key)
    if mapping is None:
        return label, label  # 変換表が無い db_key はラベルをそのままプロパティ名にする（本番と同じ簡易挙動）
    mapped = mapping.get(label)
    return label, (mapped[0] if mapped else None)


def convert_with_production_path(
    module: str, zoho_id: str, values: Mapping[str, Any], occurred_at: datetime, *, store, notion_client, zoho_client
) -> dict[str, Any]:
    """本番の変換（`zoho_payload_to_sync_events`）に通し、Notion プロパティ名 → 値 を返す。
    変換表に無い項目・未解決のリレーション・上書き防止ガードで落ちた項目は結果に含まれない。"""
    from src.sync_engine.webhook_handlers.zoho_webhook import zoho_payload_to_sync_events

    payload = build_notification_payload(module, zoho_id, values, occurred_at)
    events = zoho_payload_to_sync_events(
        payload, {}, id_mapping_store=store, notion_client=notion_client, zoho_client=zoho_client
    )
    return dict(events[0].properties) if events else {}


def plan_record(
    record: RecordReplay,
    *,
    zoho,
    notion_client,
    store,
    since: datetime,
    until: datetime | None,
    now: datetime,
) -> None:
    """1 レコード分の読み取りと分類（書き込みはしない）。失敗は record.error に残して呼び出し元へ返す。"""
    from src.sync_engine.clients._notion_keys import NOTION_LAST_EDITED_TIME_KEY

    try:
        record.planned_at = now.isoformat()
        entries = fetch_timeline(zoho, record.module, record.zoho_id, since)
        changes = changed_fields_from_timeline(entries, since, until)
        after_until = fields_changed_after(entries, until)
        raw = zoho.get_record(record.module, record.zoho_id)
        if raw is None:
            record.error = "zoho_record_not_found"
            return
        record.label = backfill.zoho_record_label(raw)
        # v2 GET の応答に Modified_Time は入っていなかった（2026-09-27 実測）ので Last_Activity_Time を見せる（表示だけ）
        record.zoho_last_activity_at = raw.get("Last_Activity_Time")
        page = notion_client.get_page(record.notion_key)
        if page is None:
            record.error = "notion_page_not_found"
            return
        last_edited = page.get(NOTION_LAST_EDITED_TIME_KEY)
        record.notion_last_edited_at = last_edited.isoformat() if isinstance(last_edited, datetime) else None

        for api_name, change in changes.items():
            change.label, change.notion_property = resolve_notion_property(record.module, record.db_key, api_name)
        # 変換表にある項目だけを本番の変換に通す（対象外の項目まで通すと「未知の項目」警告が毎件出る）
        values_in_record = {api: raw[api] for api, c in changes.items() if c.notion_property is not None and api in raw}
        converted = convert_with_production_path(
            record.module, record.zoho_id, values_in_record, now,
            store=store, notion_client=notion_client, zoho_client=zoho,
        )
        for api_name, change in sorted(changes.items()):
            in_record = api_name in raw
            change.zoho_value = raw.get(api_name)
            is_converted = change.notion_property is not None and change.notion_property in converted
            if is_converted:
                change.converted_value = converted[change.notion_property]
                change.notion_value = page.get(change.notion_property)
            change.drifted_before = bool(
                is_converted and not change.order_uncertain and change.display_old
                and change.notion_value not in (None, "") and str(change.notion_value) != change.display_old
            )
            change.status = classify_field(
                change,
                notion_last_edited_at=last_edited if isinstance(last_edited, datetime) else None,
                in_record=in_record,
                mapped=change.notion_property is not None,
                converted=is_converted,
                changed_after_until=api_name in after_until,
            )
            record.fields.append(change)
    except Exception as exc:  # 1 件の失敗で全体を止めない
        record.error = repr(exc)[:300]
        logger.exception("plan failed for %s %s", record.module, record.zoho_id)
    finally:
        record.outcome = record_outcome(record)


# ---------------------------------------------------------------------------------------------
# 収集と実行
# ---------------------------------------------------------------------------------------------


def collect_mapped(zoho, store, modules: Mapping[str, str], since: datetime, only_ids: Sequence[str] | None) -> list[RecordReplay]:
    """モジュールごとに Zoho の変更 ID を取り、対応表にあるものだけを返す（読み取りだけ）。"""
    records: list[RecordReplay] = []
    original_headers = backfill._with_if_modified_since(zoho, since)
    try:
        for module, db_key in modules.items():
            ids = backfill.fetch_zoho_ids_modified_since(zoho, module, since)
            mapped = backfill.load_mapped_zoho_ids(store, db_key)
            wanted = [i for i in ids if i in mapped and (not only_ids or i in only_ids)]
            print(f"[{module} → {db_key}] Zoho 変更 {len(ids)} 件 / 対応表にある（今回の対象）{len(wanted)} 件 / "
                  f"対応表に無い（backfill の担当）{sum(1 for i in ids if i not in mapped)} 件")
            records.extend(RecordReplay(zoho_id=i, module=module, db_key=db_key, notion_key=mapped[i]) for i in wanted)
    finally:
        zoho._headers = original_headers
    return records


def notion_page_unchanged_since_plan(record: RecordReplay, notion_client) -> bool:
    """書く直前に Notion ページを読み直し、分類したときから動いていないことを確かめる。
    分類（全件の読み取り）と書き込みの間は数分〜数十分あき、その間の Notion 側の編集はイベント時刻を
    「今」にする設計では守れない（shirokuma-sec レビュー BLOCKER、2026-09-27）。
    最終更新時刻は分単位に丸められるので、時刻だけでは同じ分の編集を見逃す。流す項目の値そのものも
    分類時と比べる（ChatGPT レビュー BLOCKER）。どちらかが動いていれば流さず、次の実行で分類し直す。
    読めなければ安全側（流さない）。"""
    from src.sync_engine.clients._notion_keys import NOTION_LAST_EDITED_TIME_KEY
    from src.sync_engine.conflict_resolver import _is_empty, _values_equal

    if record.notion_last_edited_at is None:
        return False
    page = notion_client.get_page(record.notion_key)
    if page is None:
        return False
    last_edited = page.get(NOTION_LAST_EDITED_TIME_KEY)
    if not isinstance(last_edited, datetime):
        return False
    if last_edited > datetime.fromisoformat(record.notion_last_edited_at):
        return False
    for f in record.replayable():
        if f.notion_property is None:
            continue
        current = page.get(f.notion_property)
        if not (_values_equal(current, f.notion_value) or (_is_empty(current) and _is_empty(f.notion_value))):
            return False
    return True


def zoho_record_unchanged_since_plan(record: RecordReplay, zoho) -> bool:
    """書く直前に Zoho のレコードと Timeline を読み直し、流す項目が分類したときから変わっていないことを
    確かめる。変わっていれば、分類時の値を「今の最新」として流すことになり、通常運用の新しい変更を
    巻き戻す（ChatGPT レビュー BLOCKER、2026-09-27）。読めなければ安全側（流さない）。"""
    if record.planned_at is None:
        return False
    raw = zoho.get_record(record.module, record.zoho_id)
    if raw is None:
        return False
    fields = record.replayable()
    for f in fields:
        if f.api_name not in raw or raw[f.api_name] != f.zoho_value:
            return False
    planned_at = datetime.fromisoformat(record.planned_at)
    entries = fetch_timeline(zoho, record.module, record.zoho_id, planned_at)
    touched = fields_changed_after(entries, planned_at)
    return not any(f.api_name in touched for f in fields)


def apply_record(record: RecordReplay, *, dispatcher, zoho, notion_client, store, now: datetime) -> None:
    """safe（--trust なら ambiguous も）な項目だけを通知の形にして、本番と同じ Dispatcher に流す。
    書く直前に Notion ページ（時刻と値）と Zoho レコード（値と Timeline）を読み直し、分類時から
    動いていたら流さない（`notion_edited_after_plan` / `zoho_edited_after_plan`。再実行すれば分類し直される）。
    読み直しから Dispatcher の書き込みまでの数秒は本番の通知と同じ窓で、Dispatcher のレコード単位
    ロックの内側で現在値を取り直す。"""
    from src.audit_log.actor_context import set_actor
    from src.sync_engine.webhook_handlers.zoho_webhook import zoho_payload_to_sync_events

    values = {f.api_name: f.zoho_value for f in record.replayable()}
    payload = build_notification_payload(record.module, record.zoho_id, values, now)
    try:
        if not notion_page_unchanged_since_plan(record, notion_client):
            record.outcome = "notion_edited_after_plan"
            return
        if not zoho_record_unchanged_since_plan(record, zoho):
            record.outcome = "zoho_edited_after_plan"
            return
        with set_actor(REPLAY_ACTOR):
            events = zoho_payload_to_sync_events(
                payload, {}, id_mapping_store=store, notion_client=notion_client, zoho_client=zoho
            )
            if not events or not events[0].properties:
                record.outcome = "nothing_converted"
                return
            result = dispatcher.dispatch(events[0])
        if result.skipped:
            record.outcome = result.reason or "skipped"
            return
        record.outcome = "updated"
        record.written = {
            p.property_name: sorted(t.value for t in p.written_tools) for p in result.properties
        }
        # 競合として捨てた値（Slack 通知は切ってあるので、結果 JSON と標準出力で人が追えるようにする）
        record.rejected = [
            {"property": p.property_name, "adopted_tool": r.adopted_tool.value, "adopted": r.adopted_value,
             "rejected_tool": r.rejected_tool.value, "rejected": r.rejected_value}
            for p in result.properties if p.resolution is not None
            for r in p.resolution.rejected
        ]
    except Exception as exc:
        record.outcome = RECORD_ERROR
        record.error = repr(exc)[:300]
        logger.exception("dispatch failed for %s %s", record.module, record.zoho_id)


JST = timezone(timedelta(hours=9))


def _fmt_time(value: str | None) -> str:
    """ISO 8601 の文字列を日本時間 "MM-DD HH:MM" にする（Zoho は +09:00、Notion は UTC で返るので揃える）。"""
    if not value:
        return "-"
    try:
        return datetime.fromisoformat(value).astimezone(JST).strftime("%m-%d %H:%M")
    except ValueError:
        return value


def print_record(i: int, total: int, record: RecordReplay) -> None:
    head = (f"  [{i}/{total}] {record.db_key} {record.zoho_id} {record.label!r} Zoho最終活動 {_fmt_time(record.zoho_last_activity_at)}"
            f" / Notion最終更新 {_fmt_time(record.notion_last_edited_at)} → {record.outcome}")
    if record.error:
        head += f"  ⚠️ {record.error}"
    print(head, flush=True)
    for f in record.fields:
        if f.status in (FIELD_UNMAPPED,):
            continue
        name = f.notion_property or f.label or f.api_name
        drift = "  ※変更前から Notion とズレていた" if f.drifted_before else ""
        print(f"        {f.status:15} {name}: {f.display_old or '(空)'} → {f.display_new or '(空)'}"
              f"  Notion現在値={f.notion_value!r} （Zoho変更 {f.audited_at.astimezone(JST).strftime('%m-%d %H:%M')}）{drift}")


def run(args: argparse.Namespace) -> int:
    load_env(apply=args.apply)
    from src.sync_engine.production_wiring import build_id_mapping_store, build_notion_clients_by_db, build_production_dispatcher
    from src.sync_engine.zoho_watch_channel import build_zoho_client_from_env

    mode = "APPLY（本番 Notion へ書き込み）" if args.apply else "DRY-RUN（読み取りだけ）"
    print(f"== {mode} since={args.since.isoformat()} until={args.until.isoformat() if args.until else '-'} ==")
    zoho = build_zoho_client_from_env()
    store = build_id_mapping_store()
    if type(store).__name__ != "NotionIdMappingStore":
        raise SystemExit(f"エラー: 対応表が本番と違う実装です（{type(store).__name__}）")
    notion_clients = build_notion_clients_by_db()
    if not notion_clients:
        raise SystemExit("エラー: Notion クライアントを作れません（NOTION_API_KEY / DB ID）")
    notion_client = next(iter(notion_clients.values()))

    records = collect_mapped(zoho, store, backfill.modules_to_db_keys(args.modules), args.since, args.zoho_ids)
    if args.limit is not None:
        records = records[: args.limit]
    forced = set(args.trust_zoho_ids)
    for r in records:
        r.forced = r.zoho_id in forced

    now = datetime.now(timezone.utc)
    print(f"== {len(records)} 件の変更履歴を読んで分類 ==")
    print("   凡例: safe=流せる / ambiguous=Notion が後から編集されている（流さない・人が見る） / already_synced=反映済み /"
          " relation_pending=--apply で名寄せを試みる / not_converted=名寄せが要るが Notion が後から編集されている /"
          " missing_in_record=Zoho の現在値に無い / changed_after_until=--until より後にも変わった")
    # 分類は読み取りだけ。--apply でも名寄せ（確認待ちキューへの書き込みを伴う）は流すレコードだけに限る
    relation_sync_for_apply = os.environ.get("RELATION_SYNC_ENABLED", "false")
    os.environ["RELATION_SYNC_ENABLED"] = "false"
    for i, r in enumerate(records, 1):
        plan_record(r, zoho=zoho, notion_client=notion_client, store=store, since=args.since, until=args.until, now=now)
        print_record(i, len(records), r)
        time.sleep(0.2)

    if args.apply:
        os.environ["RELATION_SYNC_ENABLED"] = relation_sync_for_apply
        dispatcher = build_production_dispatcher(id_mapping_store=store, zoho_client=zoho, slack_notifier=None)
        targets = [r for r in records if r.outcome == RECORD_READY]
        print(f"== {len(targets)} 件を Dispatcher に流す ==")
        for i, r in enumerate(targets, 1):
            apply_record(r, dispatcher=dispatcher, zoho=zoho, notion_client=notion_client, store=store, now=datetime.now(timezone.utc))
            print(f"  [{i}/{len(targets)}] {r.db_key} {r.zoho_id} → {r.outcome} {r.written or ''}", flush=True)
            for rej in r.rejected:
                print(f"        競合で捨てた値: {rej['property']} {rej['rejected_tool']}={rej['rejected']!r} → {rej['adopted_tool']}={rej['adopted']!r}", flush=True)
            time.sleep(0.4)

    outcomes = Counter(r.outcome for r in records)
    field_status = Counter(f.status for r in records for f in r.fields)
    print("== レコード:", dict(outcomes))
    print("== 項目:", dict(field_status))
    review = [r for r in records if r.fields_with(FIELD_AMBIGUOUS) and not r.forced]
    if review:
        print(f"⚠️  人が見るもの（Notion 側が後から編集されている）{len(review)} 件。Zoho の値でよければ --apply --trust-zoho-id <ID> で流せる")
        for r in review:
            print(f"      {r.db_key} {r.zoho_id} {r.label!r}: " + ", ".join(f.notion_property or f.api_name for f in r.fields_with(FIELD_AMBIGUOUS)))

    report = {
        "mode": "apply" if args.apply else "dry-run",
        "since": args.since.isoformat(),
        "until": args.until.isoformat() if args.until else None,
        "records": [_record_to_json(r) for r in records],
        "summary": {"records": dict(outcomes), "fields": dict(field_status)},
    }
    out_dir = Path(args.report_dir) if args.report_dir else Path.home() / ".local" / "state" / f"crm-sfa-zoho-replay-{datetime.now().strftime('%Y%m%d')}"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{'apply' if args.apply else 'dry-run'}-{datetime.now().strftime('%H%M%S')}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(f"結果 JSON: {out}")
    code = exit_code_for(records, apply=args.apply)
    if code:
        print(f"⚠️  終了コード {code}（1=error あり / 3=流せなかった件あり＝Dispatcher のスキップ・notion_edited_after_plan・"
              f"zoho_edited_after_plan・変換で全項目が落ちた nothing_converted）。結果 JSON の outcome で対象を確認すること")
    return code


def _record_to_json(record: RecordReplay) -> dict[str, Any]:
    data = asdict(record)
    for f in data["fields"]:
        f["audited_at"] = f["audited_at"].isoformat() if isinstance(f["audited_at"], datetime) else f["audited_at"]
    return data


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    # ページを読むたびに formula / rollup / files 型を「解釈できない」と警告する（想定内・毎件 8 行）ので黙らせる
    logging.getLogger("src.sync_engine.clients.notion_client").setLevel(logging.ERROR)
    raise SystemExit(run(parse_args()))
