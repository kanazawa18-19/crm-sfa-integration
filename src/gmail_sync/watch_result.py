"""通知期限更新の集計。個人を識別する値や外部例外を持たない。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class WatchRenewalProgress:
    total: int | None = None
    attempted: int = 0
    renewed: int = 0
    failed: int = 0
    skipped: int = 0

    def summary(self) -> dict:
        return {
            "total": self.total,
            "attempted": self.attempted,
            "renewed": self.renewed,
            "failed": self.failed,
            "skipped": self.skipped,
            "in_flight": self.attempted - self.renewed - self.failed,
            "remaining": None if self.total is None else self.total - self.attempted - self.skipped,
            "skip_reasons": {"not_due": self.skipped},
        }

    def outcome(self) -> str:
        if self.failed:
            return "partial_failure" if self.renewed else "failed"
        return "skipped" if not self.renewed else "success"
