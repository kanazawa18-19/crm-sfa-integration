from datetime import datetime, timezone
from scripts.compare_gmail_gas_ingest import summarize_observations

START = datetime(2026, 10, 1, tzinfo=timezone.utc)
END = datetime(2026, 10, 2, tzinfo=timezone.utc)


def batch(status):
    return dict(observationKind='natural', scanId='test-scan', dryRun=True, after=int(START.timestamp()), before=int(END.timestamp()),
                observedAt='2026-10-02T00:00:00Z',
                results=[dict(id='sample', status=status, internal_date_ms=int(START.timestamp()*1000))])


def test_途中のバッチ成功を全走査完了にしない():
    result = summarize_observations([batch('existing')], START, END)
    assert result[0] == {'sample'}
    assert result[-1] is False


def test_明示的な全走査完了だけを期間照合に使う():
    completed = dict(observationKind='natural', scanId='test-scan', dryRun=True, after=int(START.timestamp()), before=int(END.timestamp()),
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


def test_補完走査を自然観測と遅延へ混ぜない():
    backfill = batch('existing')
    backfill.update(observationKind='backfill', scanComplete=True, completedAt='2026-10-02T00:00:01Z')
    result = summarize_observations([backfill], START, END)
    assert result[0] == set() and result[-1] is False
    assert summarize_observations([backfill], START, END, observation_kind='backfill')[0] == {'sample'}


def test_遡及期間と未来時刻を正常遅延ゼロにしない():
    entry = batch('existing')
    assert summarize_observations([entry], START, END, latency_start=END)[4] == {}
    entry['observedAt'] = '2026-09-30T23:00:00Z'
    assert summarize_observations([entry], START, END)[4] == {}


def test_旧ログは自然観測のID照合や完走や遅延へ算入しない():
    legacy = batch('existing')
    legacy.update(scanComplete=True, completedAt='2026-10-02T00:00:01Z')
    legacy.pop('observationKind')
    assert summarize_observations([legacy], START, END, latency_start=START)[0] == set()
    assert summarize_observations([legacy], START, END, latency_start=START)[-1] is False
    legacy['observationKind'] = 'natural'
    legacy.pop('scanId')
    assert summarize_observations([legacy], START, END, latency_start=START)[4] == {}


def test_自然観測開始を省略したCLIはDB照会前に拒否(monkeypatch):
    import pytest
    from scripts.compare_gmail_gas_ingest import main
    monkeypatch.setattr('sys.argv', ['compare', '/not-read.jsonl', '--rep-email', 'rep@example.com',
                                   '--start', START.isoformat(), '--end', END.isoformat()])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
