"""専用GASから固定された承認済み計画だけを実行する。"""
import logging
from fastapi import APIRouter, Depends, HTTPException, Query
from src.contact_name_job.auth import verify_gas_identity
from src.contact_name_job.repository import Repository
from src.contact_name_job.worker import run_shard

router = APIRouter(prefix='/api/contact-name-job', dependencies=[Depends(verify_gas_identity)])
logger = logging.getLogger(__name__)


@router.get('/status')
def status():
    try:
        return Repository().status()
    except Exception as exc:
        logger.warning('姓名補完の状態取得失敗: %s', type(exc).__name__)
        raise HTTPException(503, '状態を取得できません') from None


@router.post('/run')
def run(shard: int = Query(ge=0, le=3)):
    try:
        result = run_shard(shard)
        logger.info('姓名補完: %s', result)
        return result
    except Exception as exc:
        logger.warning('姓名補完の実行失敗: %s', type(exc).__name__)
        raise HTTPException(503, '次回の定期実行で再試行します') from None
