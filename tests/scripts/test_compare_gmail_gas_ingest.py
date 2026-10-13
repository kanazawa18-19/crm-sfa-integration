from datetime import datetime, timezone
from scripts.compare_gmail_gas_ingest import summarize_observations

START = datetime(2026, 10, 1, tzinfo=timezone.utc)
END = datetime(2026, 10, 2, tzinfo=timezone.utc)


def batch(status):
    return dict(dryRun=True, after=int(START.timestamp()), before=int(END.timestamp()),
                observedAt='2026-10-02T00:00:00Z',
                results=[dict(id='sample', status=status, internal_date_ms=int(START.timestamp()*1000))])


def test_途中のバッチ成功を全走査完了にしない():
    result = summarize_observations([batch('existing')], START, END)
    assert result[0] == {'sample'}
    assert result[-1] is False


def test_明示的な全走査完了だけを期間照合に使う():
    completed = dict(dryRun=True, after=int(START.timestamp()), before=int(END.timestamp()),
                     scanComplete=True, completedAt='2026-10-02T00:00:01Z')
    assert summarize_observations([completed], START, END)[-1] is True
    completed.pop('scanComplete')
    assert summarize_observations([completed], START, END)[-1] is False


def test_後続成功で現在失敗から外れログ順が逆でも時刻で判定():
    success = batch('existing')
    success['observedAt'] = '2026-10-02T00:05:00Z'
    for previous in ('unmatched', 'effect_failed'):
        result = summarize_observations([success, batch(previous)], START, END)
        assert result[0] == {'sample'}
        assert result[1] == result[2] == set()


def test_観測成功で本取込みの失敗を解消したことにしない():
    failed = batch('effect_failed')
    failed['dryRun'] = False
    observed = batch('existing')
    observed['observedAt'] = '2026-10-02T00:05:00Z'
    result = summarize_observations([failed, observed], START, END, dry_run=False)
    assert result[2] == {'sample'}
    assert result[0] == set()


def test_異なるタイムゾーンの表記も実時刻で並べる():
    old = batch('effect_failed')
    old['observedAt'] = '2026-10-02T09:00:00+09:00'
    new = batch('existing')
    new['observedAt'] = '2026-10-02T00:01:00Z'
    result = summarize_observations([new, old], START, END)
    assert result[0] == {'sample'}
    assert result[2] == set()
