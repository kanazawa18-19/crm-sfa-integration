"""専用GASから所属根拠の巡回と状態確認を行う。"""
import logging
from fastapi import APIRouter, Depends, HTTPException
from src.contact_affiliation_job.auth import verify_gas_identity
from src.contact_affiliation_job.repository import Repository
from src.contact_affiliation_job.worker import run

router = APIRouter(prefix='/api/contact-affiliation-job', dependencies=[Depends(verify_gas_identity)])
logger = logging.getLogger(__name__)


@router.get('/status')
def status():
    try:
        return Repository().status()
    except Exception as exc:
        logger.warning('所属補完の状態取得失敗: %s', type(exc).__name__)
        raise HTTPException(503, '状態を取得できません') from None


@router.post('/run')
def tick():
    try:
        return run()
    except Exception as exc:
        logger.warning('所属補完の実行失敗: %s', type(exc).__name__)
        raise HTTPException(503, '次回の定期実行で再試行します') from None


@router.post('/discover')
def discover():
    try:
        return run(discover_only=True)
    except Exception as exc:
        logger.warning('所属補完の巡回失敗: %s', type(exc).__name__)
        raise HTTPException(503, '次回の巡回で再試行します') from None
