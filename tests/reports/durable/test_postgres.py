"""収集ページとしおり・送達予約を localhost の実DBで照合する。"""
from pathlib import Path
from datetime import date, datetime, timezone
import pytest
import psycopg
from tests.record_merge.test_postgres import local
from src.reports.durable.journal import ReportJournal
from src.sync_operations.product_holds import connect


@pytest.fixture
def report(local):
    with connect() as conn:
        conn.execute(Path('dashboard/prisma/migrations/20260928080000_report_delivery/migration.sql').read_text())
    journal = ReportJournal()
    journal.ensure(date(2026,9,28), datetime(2026,9,28,10,tzinfo=timezone.utc))
    return journal


def test_duplicate_page_upsert_and_cursor_are_atomic(report):
    job = report.next_job()
    report.save_page(job, [{'id':'p','properties':{'案件名':{'type':'title','title':[]}}}], {'cursor':'next'}, False)
    job = report.next_job()
    report.save_page(job, [{'id':'p','created_time':None,'properties':{'案件名':{'type':'title','title':[{'plain_text':'更新'}]}}}], {'cursor':'last'}, False)
    assert report.pages(job['reportDate'], 'project') == [{'id':'p','created_time':None,'properties':{'案件名':{'type':'title','title':[{'plain_text':'更新'}]}}}]
    with connect() as conn:
        conn.execute('ALTER TABLE "ReportCollection" ADD CONSTRAINT synthetic_stop CHECK (false) NOT VALID')
    with pytest.raises(psycopg.errors.CheckViolation):
        report.save_page(report.next_job(), [{'id':'q','properties':{}}], {}, True)
    assert report.pages(job['reportDate'], 'project') == [{'id':'p','created_time':None,'properties':{'案件名':{'type':'title','title':[{'plain_text':'更新'}]}}}]
    assert report.next_job()['cursor'] == {'cursor':'last'}


def test_delivery_reservation_survives_completion_write_failure(report):
    day = date(2026,9,28)
    report.reserve(day, 'daily', 'destinationhash', 'bodyhash')
    with connect() as conn:
        conn.execute('ALTER TABLE "ReportDelivery" ADD CONSTRAINT synthetic_stop CHECK (false) NOT VALID')
    with pytest.raises(psycopg.errors.CheckViolation): report.delivered(day, 'daily')
    assert report.delivery(day, 'daily')['state'] == 'reserved'


def test_repeated_failure_does_not_block_later_dates(report):
    day = date(2026,9,28)
    report.ensure(date(2026,9,29), datetime(2026,9,29,10,tzinfo=timezone.utc))
    for _ in range(2): report.error(day, '合成取得失敗')
    assert report.next_job()['reportDate'] == day
    report.error(day, '合成取得失敗')
    assert report.get(day)['phase'] == 'held'
    assert report.next_job()['reportDate'] == date(2026,9,29)


def test_successful_collection_resets_consecutive_failure_count(report):
    day = date(2026,9,28)
    for _ in range(2): report.error(day, '合成取得失敗')
    report.save_page(report.get(day), [{'id': 'recovered','properties':{}}], {'cursor': 'next'}, False)
    report.error(day, '合成取得失敗')
    assert report.get(day)['retryCount'] == 1
    assert report.get(day)['phase'] == 'project'


def test_missing_dates_are_visible_but_never_sent_automatically(report):
    report.finish(date(2026,9,28))
    report.ensure(date(2026,9,30), datetime(2026,9,30,10,tzinfo=timezone.utc))
    report.detect_missing_dates(date(2026,10,1))
    report.detect_missing_dates(date(2026,10,1))
    assert report.get(date(2026,9,29))['phase'] == 'held'
    assert report.get(date(2026,9,29))['error'].startswith('当日の収集')
    assert report.next_job()['reportDate'] == date(2026,9,30)
    assert report.get(date(2026,9,27)) is None
    assert report.get(date(2026,10,1)) is None


def test_missing_date_detection_is_bounded_and_does_not_backfill_initial_install(report):
    with connect() as conn:
        conn.execute('DELETE FROM "ReportCollection"')
    report.detect_missing_dates(date(2026,9,28))
    assert report.next_job() is None
    report.ensure(date(2026,9,1), datetime(2026,9,1,10,tzinfo=timezone.utc))
    report.detect_missing_dates(date(2026,9,28))
    assert report.get(date(2026,9,20)) is None
    assert report.get(date(2026,9,21))['phase'] == 'held'


def test_finish_keeps_latest_and_held_pages_and_all_deliveries(report):
    from datetime import timedelta
    first = date(2026,9,28)
    for offset in range(3):
        day = first + timedelta(days=offset)
        report.ensure(day, datetime(2026,9,28,10,tzinfo=timezone.utc))
        report.save_page(report.get(day), [{'id':str(offset),'properties':{}}], {}, False)
        if offset != 1:
            report.reserve(day, 'daily', 'destination', 'body')
            report.delivered(day, 'daily')
            report.finish(day)
        else:
            report.error(day, '保留', held=True)
    assert report.pages(first, 'project') == []
    assert len(report.pages(first+timedelta(days=1), 'project')) == 1
    assert len(report.pages(first+timedelta(days=2), 'project')) == 1
    assert report.delivery(first, 'daily')['state'] == 'delivered'


def test_collection_read_preserves_all_pages_across_fetch_boundaries(report):
    job = report.next_job()
    report.save_page(job, [{'id': f'p{i:05}', 'properties': {}} for i in range(2103)], {}, False)
    pages = report.pages(job['reportDate'], 'project')
    assert [p['id'] for p in pages] == [f'p{i:05}' for i in range(2103)]
