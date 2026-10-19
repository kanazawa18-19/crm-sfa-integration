"""専用GASのGoogle署名付きIDと実行者だけを許可する。秘密の貼付けは不要。"""
import os
from fastapi import Header, HTTPException
from google.auth.transport.requests import Request
from google.oauth2.id_token import verify_oauth2_token


def verify_gas_identity(authorization: str | None = Header(default=None)):
    audience = os.environ.get('ATTACHMENT_SYNC_GAS_AUDIENCE')
    subject = os.environ.get('ATTACHMENT_SYNC_GAS_SUBJECT')
    if not audience or not subject or not authorization or not authorization.startswith('Bearer ') or len(authorization) > 8192:
        raise HTTPException(401, 'unauthorized')
    try:
        request = Request()
        claims = verify_oauth2_token(authorization[7:], lambda *a, **kw: request(*a, **{**kw, 'timeout': 10}),
                                    audience=audience, clock_skew_in_seconds=30)
        if claims.get('sub') != subject:
            raise ValueError('実行者が異なります')
    except Exception:
        raise HTTPException(401, 'unauthorized') from None
