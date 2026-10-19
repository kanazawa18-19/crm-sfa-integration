"""専用GASだけが添付同期を呼び出す。既定は停止。"""
import logging
import os
from fastapi import APIRouter, Depends, HTTPException, Query
from src.attachment_sync.domain import Held
from src.attachment_sync.auth import verify_gas_identity
from src.attachment_sync.repository import Repository
from src.attachment_sync.worker import run

router = APIRouter(prefix='/api/attachment-sync', dependencies=[Depends(verify_gas_identity)])


def safely(fn):
    try:
        return fn()
    except Held as exc:
        raise HTTPException(409,str(exc)) from None
    except Exception as exc:
        logging.getLogger(__name__).warning('添付同期失敗: %s', type(exc).__name__)
        raise HTTPException(503, '添付同期の状態を確認してください') from None


@router.get('/status')
def status():
    def read():
        with Repository.locked() as repo:
            if repo is None:
                return {'status': 'busy'}
            return {'enabled': os.environ.get('ATTACHMENT_SYNC_ENABLED') == 'true', **repo.status()}
    return safely(read)


@router.post('/run')
def tick():
    return safely(run)


@router.post('/preview')
def preview():
    return safely(lambda: run(dry_run=True))

@router.get('/problems')
def problems(offset: int = Query(default=0, ge=0, le=1000000)):
    def read():
        with Repository.locked() as repo:
            if repo is None:return {'status':'busy'}
            items=repo.problems(offset)
            for item in items:
                asset=repo.asset(item['assetId']) if item['assetId'] else None
                item['delivery']=(asset or {}).get('receipts',{})
            return {'items':items,'limit':100,'next_offset':offset+100 if len(items)==100 else None}
    return safely(read)


@router.post('/retry/{source_id}')
def retry(source_id: str):
    import re
    if not re.fullmatch('[a-f0-9]{64}',source_id):raise HTTPException(400,'添付IDの形式が不正です')
    def apply():
        with Repository.locked() as repo:
            if repo is None:return {'status':'busy'}
            return {'status':'queued' if repo.retry_held(source_id) else 'unchanged'}
    return safely(apply)
