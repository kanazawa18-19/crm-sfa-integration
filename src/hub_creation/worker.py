"""照合途中の登録だけを再開する。結果不明の外部作成は対象にしない。"""
from datetime import datetime, timezone
from src.db_schema.base import Tool
from src.hub_creation.scan_journal import PostgresScanJournal
from src.infrastructure.http_budget import http_budget, remaining, HttpBudgetExceeded
from src.sync_engine.sync_event import SyncEvent


def drain_creation_scans(service, *, journal=None):
    if service is None:
        return {'disabled': True}
    journal = journal or PostgresScanJournal()
    result = {'processed': 0, 'completed': 0, 'held': 0}
    with http_budget(240):
        for row in journal.due(5):
            try:
                if remaining() < 30:
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
