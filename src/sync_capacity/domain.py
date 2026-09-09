"""外部I/Oに依存しないジョブの識別・結果判定。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Protocol

MAX_PAYLOAD_BYTES = 256 * 1024
SOURCES = frozenset({"notion", "kintone", "zoho", "spreadsheet", "spreadsheet-outbox-drain"})


class CapacityUnavailable(Exception):
    """保存・実行枠の確実性を確認できない。DB経路に迂回しない。"""


class PayloadTooLarge(ValueError):
    """認証情報除去後の保管内容が受付上限を超えた。"""


class PayloadConflict(Exception):
    """既存のイベントIDに異なる内容が届いた。"""


class OwnerMismatch(Exception):
    """実行枠とジョブの所有者が一致しない。"""


@dataclass(frozen=True)
class Submission:
    job_id: str
    source: str
    payload_hash: str
    event: dict[str, Any]


def submission(source: str, payload: dict[str, Any], sync_system_id: str | None,
               now: float, *, receipt_id: str | None = None) -> Submission:
    """認証情報を除去した内容を識別する。受信時補完値はhashに混ぜない。"""
    if source not in SOURCES:
        raise ValueError("unknown source")
    clean = dict(payload)
    # 接続の認証値を永続化しない。業務プロパティ内の同名項目は対象外。
    for key in ("token", "verification_token", "secret", "authorization"):
        clean.pop(key, None)
    canonical = json.dumps({"payload": clean, "sync_system_id": sync_system_id},
                           sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    if len(canonical.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise PayloadTooLarge("payload too large")
    if source == "notion":
        identity_payload = dict(clean)
        # 再送回数だけの変化は別内容ではない。元の保存本文・署名対象は変えない。
        identity_payload.pop("attempt_number", None)
        canonical = json.dumps({"payload": identity_payload, "sync_system_id": sync_system_id},
                               sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    # レコードIDは変更ごとに同じなので使わない。Notion/Kintoneの通知IDだけを採用。
    stable_id = clean.get("id") if source in {"notion", "kintone"} else None
    identity = str(stable_id) if stable_id else digest
    undated_notion = source == "notion" and not clean.get("id") and not clean.get("timestamp")
    if (source == "zoho" and clean.get("server_time") is None) or undated_notion:
        # 通知時刻が無い場合、同じ内容の将来の別編集を永久に重複扱いにしない。
        if not receipt_id:
            raise ValueError("receipt ID required for unidentified notification")
        identity = receipt_id
    job_id = hashlib.sha256(f"{source}:{identity}".encode()).hexdigest()
    if source == "zoho" and clean.get("server_time") is None:
        clean["server_time"] = int(now * 1000)
    headers = {"X-Sync-System-ID": sync_system_id} if sync_system_id else {}
    return Submission(job_id, source, digest,
                      {"body": json.dumps(clean, ensure_ascii=False), "headers": headers})


@dataclass(frozen=True)
class Claim:
    job_id: str
    owner: str
    slot: str
    source: str
    event: dict[str, Any]


class JobStore(Protocol):
    def enqueue(self, request: Submission, now: float) -> str: ...
    def claim(self, owner: str, now: float) -> Claim | None: ...
    def finish(self, claim: Claim, state: str, reason: str, now: float) -> None: ...


# 不明なスキップは成功扱いにしない。追加する際は副作用と再処理の安全性を確認する。
SAFE_SKIPS = frozenset({"duplicate_event", "not_a_synced_database", "page_not_found",
                        "own_system_write", "stale_event", "own_system_event"})


def result_state(result: dict[str, Any], *, partial: bool = False) -> tuple[str, str]:
    if partial or result.get("queue_needs_attention"):
        return "needs_attention", "partial_processing"
    if result.get("statusCode") != 200:
        return "needs_attention", "handler_error"
    try:
        body = json.loads(result.get("body") or "{}")
    except (ValueError, TypeError):
        return "needs_attention", "invalid_handler_result"
    if not isinstance(body, dict):
        return "needs_attention", "invalid_handler_result"
    parts = body.get("results", [body])
    if not isinstance(parts, list):
        return "needs_attention", "invalid_handler_result"
    for part in parts:
        if not isinstance(part, dict):
            return "needs_attention", "invalid_handler_result"
        skipped = part.get("skipped")
        if skipped and skipped is not True and skipped not in SAFE_SKIPS:
            return "needs_attention", "unconfirmed_skip"
    return "completed", "processed"
