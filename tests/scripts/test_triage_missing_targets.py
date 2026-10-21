from datetime import datetime, timezone, timedelta
import pytest
from scripts.triage_missing_targets import classify_gaps

NOW = datetime(2026, 10, 21, tzinfo=timezone.utc)


def inventory():
    return {'db': 'project', 'observed_at': NOW.isoformat(), 'scan_started_at': NOW.isoformat(), 'resumed': False, 'missing_targets': [
        {'id': 'p', 'target': 'zoho', 'classification': 'live_verification_required'}]}


@pytest.mark.parametrize('state,expected', [('reserved', 'creation_result_unknown'),
    ('created', 'mapping_restore_requires_readback'), ('blocked', 'input_or_identity_review_required'),
    ('other', 'unknown_attempt_state')])
def test_attempt_never_authorizes_blind_recreation(state, expected):
    attempts = [{'dbKey': 'project', 'sourceKey': 'notion:p', 'target': 'zoho', 'state': state}]
    row = classify_gaps(inventory(), attempts, now=NOW)['missing_targets'][0]
    assert row['classification'] == expected
    assert row['automatic_recovery'] is False


def test_sheet_origin_uses_original_creation_source():
    attempts = [{'dbKey': 'project', 'sourceKey': 'sheet:s', 'target': 'notion', 'state': 'created', 'externalId': 'p'},
                {'dbKey': 'project', 'sourceKey': 'sheet:s', 'target': 'zoho', 'state': 'reserved'}]
    assert classify_gaps(inventory(), attempts, now=NOW)['counts'] == {'creation_result_unknown': 1}
    attempts.append({**attempts[0], 'sourceKey': 'sheet:other'})
    assert classify_gaps(inventory(), attempts, now=NOW)['counts'] == {'ambiguous_creation_source': 1}


def test_stale_inventory_rejected():
    with pytest.raises(ValueError):
        classify_gaps(inventory(), [], now=NOW + timedelta(hours=6, seconds=1))


@pytest.mark.parametrize('change', [{'resumed': True}, {'scan_started_at': (NOW - timedelta(days=1)).isoformat()},
                                    {'scan_started_at': (NOW + timedelta(seconds=1)).isoformat()}])
def test_resumed_or_old_scan_cannot_refresh_its_freshness(change):
    with pytest.raises(ValueError):
        classify_gaps({**inventory(), **change}, [], now=NOW)


def test_unrecorded_intent_and_duplicate_mapping_stay_unresolved():
    assert classify_gaps(inventory(), [], now=NOW)['counts'] == {'registration_intent_unconfirmed': 1}
    value = inventory()
    value['missing_targets'][0]['classification'] = 'duplicate_mapping'
    assert classify_gaps(value, [], now=NOW)['counts'] == {'duplicate_mapping': 1}
