"""専用GASの公開aud/subがそろった実行者だけを受け入れる。"""
import pytest
from fastapi import HTTPException
from src.contact_affiliation_job import auth


def test_auth_rejects_unconfigured(monkeypatch):
    monkeypatch.delenv('CONTACT_AFFILIATION_GAS_AUDIENCE', raising=False)
    with pytest.raises(HTTPException):
        auth.verify_gas_identity('Bearer synthetic')


def test_auth_checks_dedicated_audience_and_subject(monkeypatch):
    monkeypatch.setenv('CONTACT_AFFILIATION_GAS_AUDIENCE', 'audience')
    monkeypatch.setenv('CONTACT_AFFILIATION_GAS_SUBJECT', 'subject')
    def verify(token, request, audience, **kwargs):
        assert audience == 'audience'
        return {'sub': 'subject'}
    monkeypatch.setattr(auth, 'verify_oauth2_token', verify)
    auth.verify_gas_identity('Bearer synthetic')
    monkeypatch.setattr(auth, 'verify_oauth2_token', lambda *args, **kwargs: {'sub': 'other'})
    with pytest.raises(HTTPException):
        auth.verify_gas_identity('Bearer synthetic')
