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
    report.save_page(job, [{'id':'p','version':1}], {'cursor':'next'}, False)
    job = report.next_job()
    report.save_page(job, [{'id':'p','version':2}], {'cursor':'last'}, False)
    assert report.pages(job['reportDate'], 'project') == [{'id':'p','version':2}]
    with connect() as conn:
        conn.execute('ALTER TABLE "ReportCollection" ADD CONSTRAINT synthetic_stop CHECK (false) NOT VALID')
    with pytest.raises(psycopg.errors.CheckViolation):
        report.save_page(report.next_job(), [{'id':'q'}], {}, True)
    assert report.pages(job['reportDate'], 'project') == [{'id':'p','version':2}]
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
    report.save_page(report.get(day), [{'id': 'recovered'}], {'cursor': 'next'}, False)
    report.error(day, '合成取得失敗')
    assert report.get(day)['retryCount'] == 1
    assert report.get(day)['phase'] == 'project'
