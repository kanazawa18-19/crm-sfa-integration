"""通知期限更新の許可項目だけを、標準出力へ1行JSONで即時記録する。"""

import json
from datetime import datetime, timezone
from threading import Lock
from uuid import uuid4

from src.gmail_sync.watch_result import WatchRenewalProgress

_write_lock = Lock()


class WatchRenewalLog:
    def __init__(self) -> None:
        self.run_id = str(uuid4())
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.progress = WatchRenewalProgress()
        self.sequence = 0

    def emit(
        self, event: str, *, status: str = "running",
        reason: str | None = None, completed: bool = False,
    ) -> dict:
        self.sequence += 1
        record = {
            "schema_version": 1,
            "job": "gmail-watch-renewal",
            "run_id": self.run_id,
            "sequence": self.sequence,
            "event": event,
            "started_at": self.started_at,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "ended_at": datetime.now(timezone.utc).isoformat() if event == "finished" else None,
            "status": status,
            "reason": reason,
            "completed": completed,
            "counts": self.progress.summary(),
        }
        # ルートロガーの既定WARNINGに左右されず、JSONの前後に接頭辞を付けない。
        with _write_lock:
            print(json.dumps(record, separators=(",", ":")), flush=True)
        return record

    def update(self, progress: WatchRenewalProgress) -> None:
        self.progress = progress
        self.emit("progress")
