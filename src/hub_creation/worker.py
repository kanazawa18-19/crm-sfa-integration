"""照合途中の登録だけを再開する。結果不明の外部作成は対象にしない。"""
from datetime import datetime, timezone
import math
from src.db_schema.base import Tool
from src.hub_creation.scan_journal import PostgresScanJournal
from src.infrastructure.http_budget import http_budget, remaining, HttpBudgetExceeded
from src.sync_engine.sync_event import SyncEvent


def _can_start_scan():
    try:
        return remaining() >= 30
    except HttpBudgetExceeded:
        return False


def drain_creation_scans(service, *, journal=None, budget_seconds=240):
    """外側の残時間と既存240秒上限の短い方で照合する。DB待機は別管理。"""
    if service is None:
        return {'disabled': True}
    if not math.isfinite(budget_seconds) or budget_seconds < 0:
        raise ValueError('照合の時間予算は有限の0以上の秒数にしてください')
    result = {'processed': 0, 'completed': 0, 'held': 0}
    with http_budget(min(240, budget_seconds)):
        # 期限不足なら一覧のDB読取も予約の延期も始めない。
        if not _can_start_scan():
            return result
        journal = journal if journal is not None else PostgresScanJournal()
        for row in journal.due(5):
            try:
                if not _can_start_scan():
                    break
                journal.defer(row['sourceKey'])
                event = SyncEvent(Tool.NOTION, row['dbKey'], row['sourceId'], datetime.now(timezone.utc))
                outcome = service.handle(event, scan_journal=journal)
                attempt = service.journal.get(row['sourceKey'], 'zoho')
                if outcome in (None, 'new_record_archived') or (attempt and attempt['state'] in ('created','reserved')):
                    result['completed'] += int(bool(attempt and attempt['state'] == 'created'))
                else:
                    result['held'] += 1
                result['processed'] += 1
            except HttpBudgetExceeded:
                break
            except Exception:
                # 外部応答や認証情報は出力しない。同期ロック競合も次回へ譲る。
                result['held'] += 1
    return result
