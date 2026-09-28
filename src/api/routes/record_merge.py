"""管理者の1組ずつの比較・統合操作。送信者は管理画面セッション由来。"""
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from src.api.dependencies import wiring_dependency
from src.api.routes.sync_review import verify_review_token
from src.record_merge.domain import MergeHeld
from src.record_merge.operations import MergeOperations
from src.sync_review.domain import ReviewForbidden
from src.sync_engine.record_sync_lock import RecordSyncBusy
from src.db_schema.base import Tool

router = APIRouter(dependencies=[Depends(verify_review_token)])


class MergeRequest(BaseModel):
    actor_id: str = Field(min_length=1, max_length=100)
    action: Literal['list', 'compare', 'prepare', 'approve', 'dismiss', 'resume', 'compare_alias', 'keep_canonical', 'compare_relation', 'approve_relation', 'resume_relation', 'compare_creation', 'dismiss_creation', 'import_creation', 'preview_abandon', 'abandon', 'compare_import_recovery', 'recover_import', 'preview_reset_import', 'reset_import']
    db_key: Literal['client_master', 'chain', 'contact', 'project', 'product', 'action'] = 'client_master'
    source_id: str = Field(default='', max_length=100)
    target_id: str = Field(default='', max_length=100)
    expected_hash: str = Field(default='', max_length=64)
    operation_id: str = Field(default='', max_length=100)
    source_db: str = Field(default='', max_length=100)
    property_name: str = Field(default='', max_length=200)
    offset: int = Field(default=0, ge=0, le=500000)
    choices: dict[str, Literal['source', 'target']] = Field(default_factory=dict)
    confirmation: str = Field(default='', max_length=100)


@router.post('/api/record-merge')
def operation(body: MergeRequest, wiring=Depends(wiring_dependency)):
    dispatcher = wiring.dispatcher._dispatcher
    target = dispatcher._targets.get(Tool.NOTION)
    clients = getattr(target, '_clients_by_db_key', {})
    operations = MergeOperations(dispatcher._store, clients, dispatcher)
    try:
        if body.action in {'preview_reset_import', 'reset_import'}:
            from src.record_merge.creation_candidates import reset_import_preview, reset_import
            if body.action == 'preview_reset_import':
                return reset_import_preview(operations, body.actor_id, body.operation_id)
            return reset_import(operations, body.actor_id, body.operation_id, body.expected_hash, body.confirmation)
        if body.action in {'compare_import_recovery', 'recover_import'}:
            from src.record_merge.creation_candidates import recovery_preview, recover_import
            from uuid import UUID
            try:
                page_id = str(UUID(body.target_id))
            except ValueError:
                raise MergeHeld('実物のNotionページIDを確認してください') from None
            if body.action == 'compare_import_recovery':
                return recovery_preview(operations, body.actor_id, body.operation_id, page_id)
            return recover_import(operations, body.actor_id, body.operation_id, page_id, body.expected_hash)
        if body.action == 'preview_abandon':
            return operations.preview_abandon(body.actor_id, body.operation_id)
        if body.action == 'abandon':
            return operations.abandon(body.actor_id, body.operation_id, body.expected_hash)
        if body.action in {'compare_creation', 'dismiss_creation', 'import_creation'}:
            from src.record_merge.creation_candidates import CreationCandidates
            candidates = CreationCandidates(operations)
            if body.action == 'compare_creation':
                return candidates.compare(body.actor_id, body.operation_id)
            return candidates.decide(body.actor_id, body.operation_id, body.expected_hash, body.action)
        if body.action in {'compare_relation', 'approve_relation', 'resume_relation'}:
            from src.record_merge.relation_decisions import RelationDecisions
            decisions = RelationDecisions(operations)
            if body.action != 'resume_relation':
                from uuid import UUID
                try:
                    body.target_id = str(UUID(body.target_id))
                except ValueError:
                    raise MergeHeld('関連先のNotionページIDを確認してください') from None
            if body.action == 'resume_relation':
                return decisions.resume(body.actor_id, body.operation_id)
            if body.action == 'compare_relation':
                return decisions.compare(body.actor_id, body.operation_id, body.target_id, body.source_db, body.property_name)
            return decisions.approve(body.actor_id, body.operation_id, body.expected_hash, body.target_id, body.source_db, body.property_name)
        if body.action == 'compare_alias':
            return operations.compare_alias_event(body.actor_id, body.operation_id)
        if body.action == 'keep_canonical':
            return operations.keep_canonical(body.actor_id, body.operation_id, body.expected_hash)
        if body.action == 'list':
            return operations.list_jobs(body.actor_id, body.offset)
        if body.action in {'compare', 'prepare'}:
            if not body.source_id or not body.target_id:
                raise MergeHeld('比較する2件を指定してください')
            from uuid import UUID
            try:
                source_id, target_id = str(UUID(body.source_id)), str(UUID(body.target_id))
            except ValueError:
                raise MergeHeld('NotionページのID形式を確認してください') from None
            args = (body.actor_id, body.db_key, source_id, target_id)
            if body.action == 'compare':
                return operations.compare(*args)
            return operations.prepare(*args, body.expected_hash, body.choices)
        if body.action == 'resume':
            return operations.resume(body.actor_id, body.operation_id)
        return operations.decide(body.actor_id, body.operation_id, body.expected_hash, body.action)
    except ReviewForbidden as exc:
        raise HTTPException(403, str(exc)) from None
    except (MergeHeld, RecordSyncBusy) as exc:
        raise HTTPException(409, str(exc)) from None
