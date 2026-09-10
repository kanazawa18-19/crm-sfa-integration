"""Firestoreの比較更新付き単一commitでジョブと固定実行枠を同時に確保する。"""

from __future__ import annotations

import os
import logging
import random
import time
import re
from dataclasses import dataclass
from typing import Any

from src.sync_capacity.telemetry import emit
from src.sync_capacity.deadline import Deadline, budgeted, current_deadline, CapacityDeadlineExceeded, STORE_SECONDS

from src.sync_capacity.domain import (
    CapacityUnavailable, Claim, OwnerMismatch, PayloadConflict, Submission,
)



@dataclass(frozen=True)
class Settings:
    project: str
    database: str
    scope: str
    slots: int

    @classmethod
    def from_env(cls) -> Settings:
        try:
            project = os.environ["SYNC_CAPACITY_FIRESTORE_PROJECT"].strip()
            database = os.environ["SYNC_CAPACITY_FIRESTORE_DATABASE"].strip()
            scope = os.environ["SYNC_CAPACITY_SCOPE"].strip()
            slots = int(os.environ["SYNC_CAPACITY_SLOTS"])
        except (KeyError, ValueError) as exc:
            raise CapacityUnavailable("capacity settings missing") from exc
        if not project or not database or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", scope):
            raise CapacityUnavailable("invalid capacity settings")
        if not 1 <= slots <= 50:
            raise CapacityUnavailable("invalid slot count")
        return cls(project, database, scope, slots)


def enabled() -> bool:
    value = os.environ.get("SYNC_CAPACITY_ENABLED", "false").strip().lower()
    if value not in {"true", "false"}:
        raise CapacityUnavailable("invalid queue flag")
    return value == "true"


class _ComparedBatch:
    """読み取った全版を前提にする。判断中はサーバーのロックを保持しない。"""

    def __init__(self, client, *, deadline=None):
        self.deadline = deadline or current_deadline.get() or Deadline.after()
        self.client = client
        self.reads = {}
        self.changes = {}

    def get(self, ref):
        if ref.path not in self.reads:
            deadline = self.deadline
            self.reads[ref.path] = ref.get(retry=None, timeout=deadline.timeout(10))
        return self.reads[ref.path]

    def create(self, ref, data):
        if self.get(ref).exists:
            raise ValueError("create requires a missing document")
        self.changes[ref.path] = (ref, "create", data)

    def update(self, ref, data):
        if not self.get(ref).exists:
            raise ValueError("update requires an existing document")
        self.changes[ref.path] = (ref, "update", data)

    def commit(self):
        from google.cloud.firestore_v1 import LastUpdateOption
        if not self.changes:
            # dry-runと照会だけの処理は書き込まない。
            return
        batch = self.client.batch()
        for path, snapshot in self.reads.items():
            change = self.changes.get(path)
            if change is None:
                # scopeの検証だけをしたenqueueにも同じ版の前提を付ける。
                # 値を変えない最小フィールド更新で、scope全体を上書きしない。
                if not snapshot.exists:
                    raise ValueError("missing read must be created in the same commit")
                values = snapshot.to_dict()
                if "version" not in values:
                    raise ValueError("read-only precondition requires scope version")
                batch.update(snapshot.reference, {"version": values["version"]},
                             option=LastUpdateOption(snapshot.update_time))
            else:
                ref, kind, data = change
                if kind == "create":
                    # SDK createはexists=falseの前提を付ける。
                    batch.create(ref, data)
                else:
                    batch.update(ref, data, option=LastUpdateOption(snapshot.update_time))
        # 応答を失ったcommitをSDK内部で再送させない。
        batch.commit(retry=None, timeout=self.deadline.timeout(10))


class FirestoreJobStore:
    def __init__(self, settings: Settings, *, client: Any = None, total_seconds: float = STORE_SECONDS,
                 atomic_diagnostics: list | None = None):
        from google.cloud import firestore
        emulator = os.environ.get("FIRESTORE_EMULATOR_HOST")
        if emulator and (os.environ.get("VERCEL") or os.environ.get("K_SERVICE")
                         or not settings.project.startswith("demo-")
                         or not re.fullmatch(r"(?:127\.0\.0\.1|localhost):[0-9]+", emulator)):
            raise CapacityUnavailable("emulator is restricted to local demo projects")
        Deadline.after(total_seconds)
        self.atomic_diagnostics = atomic_diagnostics
        self.total_seconds = total_seconds
        self.settings = settings
        self.client = client if client is not None else firestore.Client(
            project=settings.project, database=settings.database)
        self.scope_ref = self.client.collection("sync_capacity_scopes").document(settings.scope)
        self.jobs = self.scope_ref.collection("jobs")

    @budgeted
    def _atomic(self, operation):
        import grpc
        from google.api_core.exceptions import Aborted, AlreadyExists, FailedPrecondition
        conflicts = (Aborted, AlreadyExists, FailedPrecondition)
        codes = {grpc.StatusCode.ABORTED, grpc.StatusCode.ALREADY_EXISTS,
                 grpc.StatusCode.FAILED_PRECONDITION}
        started = time.monotonic()
        deadline = current_deadline.get()
        events = []
        def diagnose(outcome, attempt, phase):
            if self.atomic_diagnostics is not None:
                self.atomic_diagnostics.append({"attempts": attempt + 1, "retries": attempt,
                    "elapsed_ms": max(0, int((time.monotonic() - started) * 1000)),
                    "outcome": outcome, "phase": phase, "conflicts": list(events)})
        for attempt in range(8):
            attempt_started = time.monotonic()
            try:
                phase = "operation"
                deadline.require(deadline.attempt_minimum)
                batch = _ComparedBatch(self.client)
                result = operation(batch)
                phase = "commit"
                batch.commit()
                diagnose("success", attempt, phase)
                return result
            except conflicts as exc:
                # 比較失敗が確定した場合だけ読み直す。通信結果不明を含む連鎖は除外。
                definite_conflict = True
                chain, seen = [exc], set()
                while chain:
                    error = chain.pop()
                    if id(error) in seen:
                        continue
                    seen.add(id(error))
                    grpc_conflict = (isinstance(error, grpc.RpcError)
                                     and callable(getattr(error, "code", None))
                                     and error.code() in codes)
                    if not (isinstance(error, conflicts) or grpc_conflict):
                        definite_conflict = False
                    chain.extend(cause for cause in (error.__cause__, error.__context__) if cause is not None)
                origin = getattr(exc, "capacity_failure_origin", "unknown")
                events.append({"attempt": attempt + 1,
                    "elapsed_ms": max(0, int((time.monotonic() - attempt_started) * 1000)),
                    "phase": phase, "definite": definite_conflict,
                    "origin": origin if origin in {"guard_version_check", "rpc_commit",
                        "rpc_run_query", "rpc_batch_get_documents"} else "unknown"})
                remaining = deadline.remaining()
                delay = random.uniform(0.03, min(0.6, 0.08 * 2 ** attempt))
                insufficient = remaining < delay + deadline.attempt_minimum
                if not definite_conflict or attempt == 7 or insufficient:
                    # 診断は固定値だけを添え、元の例外・再試行判定を保つ。
                    exc.capacity_atomic = {
                        "version": 2,
                        "attempts": attempt + 1,
                        "elapsed_ms": min(86_400_000, max(0, int((time.monotonic() - started) * 1000))),
                        "phase": phase,
                        "stop_reason": ("non_conflict_chain" if not definite_conflict else
                                        "attempt_limit" if attempt == 7 else
                                        "total_deadline" if remaining <= 0 else "attempt_budget"),
                    }
                    diagnose("failed", attempt, phase)
                    raise
                try:
                    deadline.sleep(delay, reserve=deadline.attempt_minimum)
                except CapacityDeadlineExceeded:
                    exc.capacity_atomic = {"version": 2, "attempts": attempt + 1,
                        "elapsed_ms": min(86_400_000, max(0, int((time.monotonic() - started) * 1000))),
                        "phase": phase, "stop_reason": "total_deadline" if deadline.remaining() <= 0 else "attempt_budget"}
                    diagnose("failed", attempt, phase)
                    raise exc from None
            except Exception:
                diagnose("failed", attempt, phase)
                raise

    def _scope(self, transaction) -> dict:
        snapshot = transaction.get(self.scope_ref)
        data = snapshot.to_dict() if snapshot.exists else None
        expected_keys = {str(n) for n in range(self.settings.slots)}
        if (not data or data.get("version") != 1
                or data.get("limit") != self.settings.slots
                or set(data.get("slots", {})) != expected_keys):
            raise CapacityUnavailable("shared scope missing or mismatched")
        return data

    @budgeted
    def initialize(self, *, apply: bool = False) -> dict:
        """運用者がDB容量を照合してから明示実行。既存scopeの上書きは禁止。"""
        desired = {"version": 1, "limit": self.settings.slots,
                   "slots": {str(n): None for n in range(self.settings.slots)}}
        def operation(tx):
            snapshot = tx.get(self.scope_ref)
            if snapshot.exists:
                self._scope(tx)
                return {"action": "already_initialized", "limit": self.settings.slots}
            if apply:
                tx.create(self.scope_ref, desired)
            return {"action": "create_scope", "apply": apply, "limit": self.settings.slots}
        return self._atomic(operation)

    @budgeted
    def enqueue(self, request: Submission, now: float, *, receipt_id: str | None = None) -> str:
        ref = self.jobs.document(request.job_id)
        correlation = {"receipt_id": receipt_id} if receipt_id is not None else {}
        def operation(tx):
            self._scope(tx)
            existing = tx.get(ref)
            if existing.exists:
                data = existing.to_dict()
                if data["payload_hash"] != request.payload_hash:
                    raise PayloadConflict("same event ID with different content")
                return data["state"], "duplicate"
            tx.create(ref, {"source": request.source, "event": request.event,
                            "payload_hash": request.payload_hash, "state": "pending",
                            "created_at": now, "available_at": now, "updated_at": now,
                            "owner": None, "slot": None, "attempts": 0})
            return "pending", "new"
        try:
            state, outcome = self._atomic(operation)
        except CapacityUnavailable:
            # scope検証で停止。この呼出しはcommit前だが、既存jobの不存在は意味しない。
            emit("enqueue_scope_unavailable", level=logging.WARNING,
                 job_id=request.job_id, source=request.source, **correlation)
            raise
        except PayloadConflict:
            emit("enqueue_conflict", level=logging.WARNING,
                 job_id=request.job_id, source=request.source, **correlation)
            raise
        except Exception:
            emit("enqueue_unconfirmed", level=logging.WARNING,
                 job_id=request.job_id, source=request.source, **correlation)
            raise
        emit("enqueue", outcome=outcome, job_id=request.job_id,
             source=request.source, state=state, **correlation)
        return state

    @budgeted
    def claim(self, owner: str, now: float) -> Claim | None:
        from google.cloud.firestore_v1.base_query import FieldFilter
        # 同時刻のScheduler呼出しが共通scopeを一斉に読む競合を分散する。
        current_deadline.get().sleep(random.uniform(0.0, 0.5),
                                     reserve=current_deadline.get().attempt_minimum)
        # 複合indexは配備準備に含める。query失敗時はDBへ進まない。
        candidates = list(self.jobs.where(filter=FieldFilter("state", "in", ["pending", "retry"]))
                          .order_by("available_at").limit(20).stream(
                              retry=None, timeout=current_deadline.get().timeout(10)))
        for candidate in candidates:
            ref = candidate.reference
            def operation(tx):
                scope = self._scope(tx)
                slot = next((key for key, value in scope["slots"].items() if value is None), None)
                if slot is None:
                    return False
                snap = tx.get(ref)
                if not snap.exists:
                    return None
                data = snap.to_dict()
                if data["state"] not in {"pending", "retry"} or data["available_at"] > now:
                    return None
                scope["slots"][slot] = {"job_id": ref.id, "owner": owner}
                tx.update(self.scope_ref, {"slots": scope["slots"]})
                tx.update(ref, {"state": "processing", "owner": owner, "slot": slot,
                                "updated_at": now, "attempts": data["attempts"] + 1})
                return Claim(ref.id, owner, slot, data["source"], data["event"])
            claimed = self._atomic(operation)
            if claimed is False:
                return None
            if claimed is not None:
                return claimed
        # 空キューでもscope不一致を見逃さない。
        self._atomic(lambda tx: self._scope(tx))
        return None

    @budgeted
    def finish(self, claim: Claim, state: str, reason: str, now: float) -> None:
        if state not in {"retry", "completed", "needs_attention"}:
            raise ValueError("invalid finish state")
        ref = self.jobs.document(claim.job_id)
        def operation(tx):
            scope = self._scope(tx)
            snapshot = tx.get(ref)
            data = snapshot.to_dict() if snapshot.exists else {}
            if (data.get("state") != "processing" or data.get("owner") != claim.owner
                    or data.get("slot") != claim.slot
                    or scope["slots"].get(claim.slot) != {"job_id": claim.job_id, "owner": claim.owner}):
                raise OwnerMismatch("job and slot owner must match")
            scope["slots"][claim.slot] = None
            tx.update(self.scope_ref, {"slots": scope["slots"]})
            # 最終ownerは調査用に残す。例外本文・業務レスポンスは保存しない。
            tx.update(ref, {"state": state, "reason": reason, "updated_at": now,
                            "available_at": now + 60 if state == "retry" else now})
        self._atomic(operation)

    @budgeted
    def observe(self) -> dict:
        """scopeの存在を読み取りで確認。観測のためのcommitは行わない。"""
        from src.sync_capacity.observation import observe_queue
        self._scope(_ComparedBatch(self.client))
        return observe_queue(self.jobs)

    @budgeted
    def recover(self, job_id: str, owner: str, *, now: float, apply: bool = False,
                stopped: bool = False) -> dict:
        """停止を外部で確認したprocessingを保全状態へ移す。自動再送しない。"""
        ref = self.jobs.document(job_id)
        def operation(tx):
            scope = self._scope(tx)
            snapshot = tx.get(ref)
            data = snapshot.to_dict() if snapshot.exists else {}
            slot = data.get("slot")
            if (data.get("state") != "processing" or not owner or data.get("owner") != owner
                    or scope["slots"].get(slot) != {"job_id": job_id, "owner": owner}):
                raise OwnerMismatch("recovery owner must match processing job and slot")
            if apply:
                if not stopped:
                    raise ValueError("confirm worker stopped before releasing its slot")
                scope["slots"][slot] = None
                tx.update(self.scope_ref, {"slots": scope["slots"]})
                tx.update(ref, {"state": "needs_attention", "reason": "manual_recovery",
                                "updated_at": now})
            return {"job_id": job_id, "owner": owner, "slot": slot,
                    "action": "needs_attention_and_release", "apply": apply}
        return self._atomic(operation)


def get_store() -> FirestoreJobStore:
    # SDKクライアントはDB接続を行わない。認証後に呼び出す。
    return FirestoreJobStore(Settings.from_env())
