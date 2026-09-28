"""管理画面の比較・見送り・承認・再開を同じロックへ接続する。"""
from contextlib import ExitStack, contextmanager, nullcontext
from src.record_merge.aliases import enabled, resolve_alias, require_record_available
from src.record_merge.application import MergeService
from src.record_merge.domain import MergeHeld, digest, stable_blocks
from src.record_merge.journal import MergeJournal
from src.record_merge.notion_gateway import NotionMergeGateway
from src.sync_engine.record_sync_lock import acquire_record_sync_lock, acquire_record_sync_locks
from src.sync_operations.product_holds import connect, require_manager


class MergeOperations:
    def __init__(self, store, clients, dispatcher=None):
        self.store = store
        self.dispatcher = dispatcher
        self.gateway = NotionMergeGateway(clients, store)
        self.journal = MergeJournal()

    @contextmanager
    def locks(self, job):
        keys = {(job['dbKey'], job['sourceId']), (job['dbKey'], job['targetId'])}
        keys.update((child['dbKey'], child['id']) for child in job.get('snapshot', {}).get('children', []))
        with acquire_record_sync_locks(self.store, keys):
            yield

    def authorize(self, actor_id, *, allow_disabled=False):
        if not enabled() and not allow_disabled:
            raise MergeHeld('統合操作は準備中です')
        with connect() as conn:
            require_manager(conn, actor_id)

    def compare(self, actor_id, db_key, source_id, target_id):
        self.authorize(actor_id)
        if source_id == target_id:
            raise MergeHeld('異なる2件を選んでください')
        job = {'dbKey': db_key, 'sourceId': source_id, 'targetId': target_id}
        with self.locks(job):
            for page_id in (source_id, target_id):
                if resolve_alias(db_key, 'notion', page_id):
                    raise MergeHeld('既に統合済みのページです。統合先を選んでください')
                require_record_available(db_key, page_id)
            snapshot = self.gateway.snapshot(db_key, source_id, target_id)
        return {'snapshot': snapshot, 'hash': digest(snapshot)}

    def prepare(self, actor_id, db_key, source_id, target_id, expected_hash, choices):
        comparison = self.compare(actor_id, db_key, source_id, target_id)
        if comparison['hash'] != expected_hash:
            raise MergeHeld('比較後に内容が変わりました。比較し直してください')
        snapshot = comparison['snapshot']
        steps = self.gateway.plan(snapshot, choices)
        return self.journal.create(snapshot, steps, actor_id)

    def decide(self, actor_id, operation_id, expected_hash, action):
        self.authorize(actor_id)
        job = self.journal.get_authorized(operation_id, actor_id)
        with self.locks(job):
            if action == 'approve':
                for page_id in (job['sourceId'], job['targetId']):
                    require_record_available(job['dbKey'], page_id)
                for child in job['snapshot'].get('children', []):
                    require_record_available(child['dbKey'], child['id'])
                actual = self.gateway.snapshot(job['dbKey'], job['sourceId'], job['targetId'])
                if digest(actual) != digest(job['snapshot']):
                    raise MergeHeld('準備後に内容が変わりました。元のページを保持します')
            self.journal.decide(operation_id, actor_id, expected_hash, action)
        if action == 'approve':
            return self.resume(actor_id, operation_id)
        return self.journal.get_authorized(operation_id, actor_id)

    def resume(self, actor_id, operation_id):
        self.authorize(actor_id, allow_disabled=True)
        return MergeService(self.journal, self.gateway, self.locks).execute(
            operation_id, actor_id, read_only=not enabled())

    def list_jobs(self, actor_id, offset=0):
        self.authorize(actor_id, allow_disabled=True)
        with connect() as conn:
            rows = conn.execute('''SELECT j.*,u.name AS "actorName",COALESCE((SELECT jsonb_agg(h) FROM (
                SELECT h."actorId",u2.name AS "actorName",h."createdAt",h."afterState" FROM "SyncOperationHistory" h
                LEFT JOIN "User" u2 ON u2.id=h."actorId" WHERE h."subjectId"=j.id
                ORDER BY h."createdAt" DESC,h.id DESC LIMIT 20) h),'[]'::jsonb) AS history
                FROM "RecordMergeJob" j LEFT JOIN "User" u ON u.id=j."actorId"
                ORDER BY j."updatedAt" DESC,j.id OFFSET %s LIMIT 51''', (offset,)).fetchall()
            candidates = conn.execute('''SELECT q.id,q."targetDbKey",q."rawValue",q."candidateNotionPageIds",q."candidateRawNames",d.state AS "decisionState"
                FROM "RelationReviewQueue" q LEFT JOIN "RelationReviewDecision" d ON d."reviewId"=q.id
                WHERE q.status='pending' ORDER BY q."createdAt",q.id OFFSET %s LIMIT 51''', (offset,)).fetchall()
            alias_events = conn.execute('SELECT * FROM "RecordMergeAliasEvent" ORDER BY "createdAt" DESC,id OFFSET %s LIMIT 51', (offset,)).fetchall()
            creation_candidates = conn.execute('SELECT id,"dbKey","sourceId",tool,"externalId",state FROM "RecordCreationCandidate" ORDER BY "updatedAt" DESC,id OFFSET %s LIMIT 51', (offset,)).fetchall()
            return {'creationCandidates': creation_candidates[:50], 'hasMoreCreationCandidates': len(creation_candidates)>50, 'items': rows[:50], 'hasMore': len(rows)>50, 'candidates': candidates[:50], 'hasMoreCandidates': len(candidates)>50, 'aliasEvents': alias_events[:50], 'hasMoreAliasEvents': len(alias_events)>50}


    def compare_alias_event(self, actor_id, event_id, *, already_locked=False):
        self.authorize(actor_id)
        from src.db_schema.base import Tool
        from src.sync_review.service import external_id
        with connect() as conn:
            event = conn.execute('SELECT * FROM "RecordMergeAliasEvent" WHERE id=%s', (event_id,)).fetchone()
        if event is None or event['state'] != 'pending':
            raise MergeHeld('未処理の旧ID変更がありません')
        mapping = self.store.get(event['targetId'])
        if mapping is None or mapping.db_key != event['dbKey']:
            raise MergeHeld('統合先の対応を確認できません')
        with (nullcontext() if already_locked else acquire_record_sync_lock(self.store, mapping.db_key, mapping.notion_key)):
            notion = self.gateway.values(mapping.db_key, self.gateway.page(mapping.db_key, mapping.notion_key))
            tool = Tool(event['sourceTool'])
            target = self.dispatcher._targets.get(tool) if self.dispatcher else None
            identifier = external_id(tool, mapping)
            current_crm = target.get_record(identifier, db_key=mapping.db_key) if target and identifier else None
            if current_crm is None:
                raise MergeHeld('正本CRMの現在値を確認できません')
            comparison = {'id': event_id, 'targetId': mapping.notion_key, 'old': event['properties'],
                          'notion': notion, 'crm': current_crm}
            return {'comparison': comparison, 'hash': digest(comparison)}

    def keep_canonical(self, actor_id, event_id, expected_hash):
        comparison = self.compare_alias_event(actor_id, event_id)
        with acquire_record_sync_lock(self.store, self.store.get(comparison['comparison']['targetId']).db_key,
                                      comparison['comparison']['targetId']):
            latest = self.compare_alias_event(actor_id, event_id, already_locked=True)
            if latest['hash'] != expected_hash:
                raise MergeHeld('比較後に正本が変わりました。比較し直してください')
            from src.record_merge.journal import history
            with connect() as conn:
                require_manager(conn, actor_id)
                updated = conn.execute('''UPDATE "RecordMergeAliasEvent" SET state='kept_current',"resolvedAt"=now()
                    WHERE id=%s AND state='pending' RETURNING id''', (event_id,)).fetchone()
                if updated is None: raise MergeHeld('この変更は処理済みです')
                history(conn, actor_id, event_id, latest['comparison'], {'state': 'kept_current'})
            return {'id': event_id, 'state': 'kept_current'}

    def preview_abandon(self, actor_id, operation_id):
        """中止は巻戻しではない。確認できた途中結果を残して同期停止を解除する。"""
        self.authorize(actor_id, allow_disabled=True)
        job = self.journal.get_authorized(operation_id, actor_id)
        with self.locks(job):
            return self._abandon_snapshot(job)

    def _abandon_snapshot(self, job):
        if job['state'] not in {'approved', 'running', 'held'}:
            raise MergeHeld('中止できる処理ではありません')
        source = self.gateway.page(job['dbKey'], job['sourceId'])
        if source.get('archived') or source.get('in_trash'):
            raise MergeHeld('元は既にアーカイブ済みです。旧ID対応を確定する再開を行ってください')
        actual = []
        for step in job['steps']:
            try:
                value = self.gateway.read(step) if step['kind'] != 'append_body' else stable_blocks(self.gateway.blocks(step['dbKey'], step['id']))
                actual.append({'step': step, 'current': value, 'readable': True})
            except Exception:
                actual.append({'step': step, 'readable': False})
        snapshot = {'id': job['id'], 'state': job['state'], 'progress': job['progress'],
                    'planHash': job['planHash'], 'sourceId': job['sourceId'], 'targetId': job['targetId'], 'actual': actual}
        return {'snapshot': snapshot, 'hash': digest(snapshot)}

    def abandon(self, actor_id, operation_id, expected_hash):
        from src.record_merge.journal import history
        self.authorize(actor_id, allow_disabled=True)
        job = self.journal.get_authorized(operation_id, actor_id)
        with self.locks(job):
            job = self.journal.get_authorized(operation_id, actor_id)
            actual = self._abandon_snapshot(job)
            if actual['hash'] != expected_hash:
                raise MergeHeld('中止確認後に内容が変わりました。比較し直してください')
            with connect() as conn:
                require_manager(conn, actor_id)
                conn.execute('UPDATE "RecordMergeJob" SET state=\'abandoned\',"updatedAt"=now() WHERE id=%s', (operation_id,))
                history(conn, actor_id, operation_id, actual['snapshot'], {'state': 'abandoned', 'partialChangesRetained': True})
        return {'id': operation_id, 'state': 'abandoned'}
