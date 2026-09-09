"""Firestoreの2文書transactionでジョブと固定実行枠を同時に確保する。"""

from __future__ import annotations

import os
import random
import time
import re
from dataclasses import dataclass
from typing import Any

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


class FirestoreJobStore:
    def __init__(self, settings: Settings, *, client: Any = None):
        from google.cloud import firestore
        emulator = os.environ.get("FIRESTORE_EMULATOR_HOST")
        if emulator and (os.environ.get("VERCEL") or os.environ.get("K_SERVICE")
                         or not settings.project.startswith("demo-")
                         or not re.fullmatch(r"(?:127\.0\.0\.1|localhost):[0-9]+", emulator)):
            raise CapacityUnavailable("emulator is restricted to local demo projects")
        self.settings = settings
        self.client = client if client is not None else firestore.Client(
            project=settings.project, database=settings.database)
        self.scope_ref = self.client.collection("sync_capacity_scopes").document(settings.scope)
        self.jobs = self.scope_ref.collection("jobs")

    def _atomic(self, operation):
        import grpc
        from google.api_core.exceptions import Aborted
        from google.cloud import firestore
        deadline = time.monotonic() + 3.0
        for attempt in range(8):
            try:
                return firestore.transactional(operation)(self.client.transaction(max_attempts=1))
            except (Aborted, ValueError) as exc:
                # SDKはcommitのABORTEDをValueErrorのcauseに包む。確実に取消済みの
                # 場合だけずらして再試行する。Timeout/Unavailable等は絶対に含めない。
                definite_abort = isinstance(exc, Aborted) or isinstance(exc.__cause__, Aborted)
                # SDKのrollback例外が元のcommit応答不明を隠している場合も再試行禁止。
                chain, seen = [exc], set()
                while chain:
                    error = chain.pop()
                    if id(error) in seen:
                        continue
                    seen.add(id(error))
                    # google-api-coreは元のgRPCエラーをcauseに残す。
                    # 生のABORTEDも許すが、通信結果不明のcodeは許さない。
                    grpc_abort = isinstance(error, grpc.RpcError) and error.code() == grpc.StatusCode.ABORTED
                    if not (isinstance(error, Aborted) or grpc_abort
                            or (isinstance(error, ValueError) and isinstance(error.__cause__, Aborted))):
                        definite_abort = False
                    chain.extend(cause for cause in (error.__cause__, error.__context__) if cause is not None)
                remaining = deadline - time.monotonic()
                if not definite_abort or attempt == 7 or remaining <= 0:
                    raise
                time.sleep(min(remaining, random.uniform(0.03, min(0.6, 0.08 * 2 ** attempt))))

    def _scope(self, transaction) -> dict:
        snapshot = self.scope_ref.get(transaction=transaction)
        data = snapshot.to_dict() if snapshot.exists else None
        expected_keys = {str(n) for n in range(self.settings.slots)}
        if (not data or data.get("version") != 1
                or data.get("limit") != self.settings.slots
                or set(data.get("slots", {})) != expected_keys):
            raise CapacityUnavailable("shared scope missing or mismatched")
        return data

    def initialize(self, *, apply: bool = False) -> dict:
        """運用者がDB容量を照合してから明示実行。既存scopeの上書きは禁止。"""
        desired = {"version": 1, "limit": self.settings.slots,
                   "slots": {str(n): None for n in range(self.settings.slots)}}
        def operation(tx):
            snapshot = self.scope_ref.get(transaction=tx)
            if snapshot.exists:
                self._scope(tx)
                return {"action": "already_initialized", "limit": self.settings.slots}
            if apply:
                tx.create(self.scope_ref, desired)
            return {"action": "create_scope", "apply": apply, "limit": self.settings.slots}
        return self._atomic(operation)

    def enqueue(self, request: Submission, now: float) -> str:
        ref = self.jobs.document(request.job_id)
        def operation(tx):
            self._scope(tx)
            existing = ref.get(transaction=tx)
            if existing.exists:
                data = existing.to_dict()
                if data["payload_hash"] != request.payload_hash:
                    raise PayloadConflict("same event ID with different content")
                return data["state"]
            tx.create(ref, {"source": request.source, "event": request.event,
                            "payload_hash": request.payload_hash, "state": "pending",
                            "created_at": now, "available_at": now, "updated_at": now,
                            "owner": None, "slot": None, "attempts": 0})
            return "pending"
        return self._atomic(operation)

    def claim(self, owner: str, now: float) -> Claim | None:
        from google.cloud.firestore_v1.base_query import FieldFilter
        # 同時刻のScheduler呼出しが共通scopeを一斉に読む競合を分散する。
        time.sleep(random.uniform(0.0, 0.5))
        # 複合indexは配備準備に含める。query失敗時はDBへ進まない。
        candidates = list(self.jobs.where(filter=FieldFilter("state", "in", ["pending", "retry"]))
                          .order_by("available_at").limit(20).stream())
        for candidate in candidates:
            ref = candidate.reference
            def operation(tx):
                scope = self._scope(tx)
                slot = next((key for key, value in scope["slots"].items() if value is None), None)
                if slot is None:
                    return False
                snap = ref.get(transaction=tx)
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

    def finish(self, claim: Claim, state: str, reason: str, now: float) -> None:
        if state not in {"retry", "completed", "needs_attention"}:
            raise ValueError("invalid finish state")
        ref = self.jobs.document(claim.job_id)
        def operation(tx):
            scope = self._scope(tx)
            snapshot = ref.get(transaction=tx)
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

    def recover(self, job_id: str, owner: str, *, now: float, apply: bool = False,
                stopped: bool = False) -> dict:
        """停止を外部で確認したprocessingを保全状態へ移す。自動再送しない。"""
        ref = self.jobs.document(job_id)
        def operation(tx):
            scope = self._scope(tx)
            snapshot = ref.get(transaction=tx)
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
