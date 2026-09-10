"""固定4job試験の開始権と、元台帳内の共有予算。"""
import hashlib
import json
import os
from pathlib import Path
import secrets
import time

from .guard import BASE, DATABASE, LEGACY_PROJECT, PROJECT, Refused

SCOPE = "trial-retry-deadline-1"
LEDGER_PATH = Path("/Users/cnctor/.local/state/crm-capacity-trial-260910/ledger.json")
# 実操作時の読取り照合が未承認のため未設定。推測で履歴を許可しない。
EXPECTED_HISTORY_SHA256 = None
BASELINE = {"reads": 4438, "writes": 423, "deletes": 0, "runtime_seconds": 5785,
            "sql_connections": 5, "sql_statements": 24}
CAPS = {"runner": {"rpc": 400, "reads": 3000, "writes": 3200, "runtime_seconds": 660},
        "observer": {"rpc": 2, "reads": 102, "writes": 0, "runtime_seconds": 60}}


def wall_time():
    return time.time()


def path_check(ledger):
    # 別台帳・symlink経由の複製を開始に使わない。ローカル試験だけ定数を差し替える。
    if ledger.path.is_symlink() or ledger.path.absolute() != LEDGER_PATH or ledger.path.resolve() != LEDGER_PATH:
        raise Refused("固定試験には元台帳だけを使用できます")


def active(data, now):
    upper = data.get("upper_bound") or {}
    if (data.get("project") != PROJECT or data.get("database") != DATABASE
            or data.get("created_at") != 1788983820 or data.get("halted")
            or not 0 <= now - data["created_at"] < 14 * 86400
            or upper.get("stage") != 2 or upper.get("usd") != 8
            or upper.get("basis") != "reserved-upper-bound"
            or data.get("cost_refresh_required")
            or (data.get("cost") or {}).get("usd", 0) + 8 >= 10):
        raise Refused("元台帳の期限・停止条件・stage2不一致")


def history(data):
    return {key: data.get(key) for key in ("target_migration", "upper_bound_transition", "created_at")}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def begin(ledger, role, now=None):
    path_check(ledger)
    now = wall_time() if now is None else now
    ticket = secrets.token_hex(32)
    def update(data):
        active(data, now)
        record = data.get("retry_trial")
        if role == "runner":
            upper = data["upper_bound"]
            names = upper.get("document_names", [])
            migration = data.get("target_migration") or {}
            transition = data.get("upper_bound_transition") or {}
            previous = transition.get("previous") or {}
            prefix = BASE + "/documents/sync_capacity_scopes/"
            if (EXPECTED_HISTORY_SHA256 is None or fingerprint(history(data)) != EXPECTED_HISTORY_SHA256
                    or record is not None or data.get("reserved") != BASELINE
                    or upper.get("rpc_reserved") != 1483 or len(names) != 90 or len(set(names)) != 90
                    or any(name.startswith(prefix + SCOPE) for name in names)
                    or not all(prefix + f"trial-concurrency-{n}" in names for n in range(1, 7))
                    or migration.get("from_project") != LEGACY_PROJECT
                    or migration.get("from_database") != "(default)"
                    or not isinstance(migration.get("at"), (int, float))
                    or transition.get("created_at") != data["created_at"]
                    or previous.get("basis") != "reserved-upper-bound"
                    or previous.get("usd") != 4 or previous.get("stage", 1) != 1
                    or not isinstance(transition.get("reserved"), dict)
                    or not isinstance(transition.get("rpc_calls"), dict)
                    or not transition.get("evidence") or not upper.get("evidence")):
                raise Refused("元台帳の固定累計・利用履歴が計画と異なります")
            record = {"history": fingerprint(history(data)), "baseline_names": list(names),
                      "children_stopped": False, "runner": None, "observer": None}
            data["retry_trial"] = record
        elif role != "observer" or not record or not record.get("children_stopped"):
            raise Refused("子の停止未確認につき観測できません")
        if record[role] is not None or record["history"] != fingerprint(history(data)):
            raise Refused("開始済み、または履歴が変更されています")
        record[role] = {"ticket": ticket, "started_at": now, "coordinator": None,
                        "children": [], "used": dict.fromkeys(CAPS[role], 0)}
    ledger.transact(update)
    ledger.retry_role, ledger.retry_ticket = role, ticket
    return ticket


def context(ledger, data, now=None):
    path_check(ledger)
    active(data, wall_time() if now is None else now)
    record = data.get("retry_trial") or {}
    role = getattr(ledger, "retry_role", None)
    part = record.get(role) if role in CAPS else None
    if (not part or part.get("finished_at") is not None or part.get("ticket") != getattr(ledger, "retry_ticket", None)
            or record.get("history") != fingerprint(history(data))):
        raise Refused("固定試験の開始権がありません")
    if role == "runner" and record.get("children_stopped"):
        raise Refused("終了済みの試験は再開できません")
    return record, part, role


def attach(ledger, role, ticket, *, child=False):
    ledger.retry_role, ledger.retry_ticket = role, ticket
    def update(data):
        record, part, _ = context(ledger, data)
        pid = os.getpid()
        if child:
            if role != "runner" or part["coordinator"] != os.getppid() or pid in part["children"] or len(part["children"]) >= 12:
                raise Refused("限定試験の子起動条件が不正")
            part["children"].append(pid)
        else:
            if part["coordinator"] is not None:
                raise Refused("調整役は一度しか起動できません")
            part["coordinator"] = pid
    ledger.transact(update)


def reserve(ledger, data, rpc, names, amounts, now):
    record, part, role = context(ledger, data, now)
    used, caps = part["used"], CAPS[role]
    additions = {"rpc": int(rpc), **amounts}
    if any(key not in caps and value for key, value in additions.items()):
        raise Refused("限定試験で許可されない予約")
    if any(used[key] + additions.get(key, 0) > cap for key, cap in caps.items()):
        raise Refused("限定試験の共有内枠超過", code="limit_exceeded")
    prefix = BASE + "/documents/sync_capacity_scopes/" + SCOPE
    if any(name != prefix and not name.startswith(prefix + "/jobs/") for name in names):
        raise Refused("限定試験以外の文書名")
    new = (set(data["upper_bound"]["document_names"]) | set(names)) - set(record["baseline_names"])
    if len(new) > 5:
        raise Refused("追加文書名は最大5件")
    # 試験側には観測用の残枠を渡さない。既存の上限も同じtransactで検査する。
    observer = CAPS["observer"] if role == "runner" else dict.fromkeys(CAPS["observer"], 0)
    if (data["upper_bound"]["rpc_reserved"] + int(rpc) + observer["rpc"] > 2000
            or data["reserved"]["reads"] + amounts.get("reads", 0) + observer["reads"] > 10000
            or data["reserved"]["runtime_seconds"] + amounts.get("runtime_seconds", 0) + observer["runtime_seconds"] > 86400):
        raise Refused("観測分を保持する既存枠が不足", code="limit_exceeded")
    for key in caps:
        used[key] += additions.get(key, 0)
