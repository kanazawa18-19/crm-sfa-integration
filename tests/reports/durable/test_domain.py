import pytest
from src.reports.durable.domain import accept_page, query_body, delivery_action, ReportHeld


def page(identifier='p', created='2026-09-01T00:00:00Z'):
    return {'id': identifier, 'created_time': created}


def test_cursor_and_incomplete_boundary_never_mark_truncated_data_complete():
    state, complete = accept_page({}, {'results':[page()], 'has_more': True, 'next_cursor': 'next', 'request_status': None})
    assert not complete and state['cursor'] == 'next'
    assert query_body(state, '2026-09-28T10:00:00Z')['start_cursor'] == 'next'
    state, complete = accept_page(state, {'results': [page('q')], 'has_more': False, 'request_status': {'type': 'incomplete'}})
    assert not complete and state['watermark'] == '2026-09-01T00:00:00Z' and 'cursor' not in state
    with pytest.raises(ReportHeld, match='進みません'):
        accept_page(state, {'results': [page()], 'has_more': False, 'request_status': {'type': 'incomplete'}})
    _, complete = accept_page(state, {'results': [page()], 'has_more': False, 'request_status': None})
    assert complete


@pytest.mark.parametrize('response', [
    {'results': [], 'has_more': True},
    {'results': [page()], 'has_more': 'false'},
    {'results': [page(created='bad')], 'has_more': False},
    {'results': [page()], 'has_more': False, 'request_status': 'bad'},
])
def test_invalid_progress_is_not_complete(response):
    with pytest.raises(ReportHeld): accept_page({}, response)


def test_unknown_delivery_and_changed_destination_are_never_retried():
    assert delivery_action(None, 'destination') == 'reserve'
    assert delivery_action({'destinationHash':'destination', 'state':'delivered'}, 'destination') == 'skip'
    for row in ({'destinationHash':'destination','state':'reserved'}, {'destinationHash':'other','state':'delivered'}):
        with pytest.raises(ReportHeld): delivery_action(row, 'destination')


def test_successful_http_status_is_required_for_delivery(monkeypatch):
    from types import SimpleNamespace
    from src.reports.durable.production import send
    import src.reports.durable.production as production
    monkeypatch.setattr(production.requests, 'post', lambda *args, **kwargs: SimpleNamespace(status_code=500))
    with pytest.raises(RuntimeError): send('https://example.invalid', '合成日報')
    monkeypatch.setattr(production.requests, 'post', lambda *args, **kwargs: SimpleNamespace(status_code=200))
    send('https://example.invalid', '合成日報')
