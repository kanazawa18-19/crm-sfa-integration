"""保存期間試算は内容を返さず、日付をパラメータで渡す。"""
from datetime import datetime, timedelta, timezone
import pytest
from scripts.report_email_body_retention import report


class Connection:
    def execute(self, query, args):
        self.query, self.args = query, args
        return self

    def fetchall(self):
        return [{'state': 'pending', 'older_than_cutoff': True, 'records': 2, 'body_bytes': 100}]


def test_report_has_no_deletion_or_body_output():
    conn = Connection()
    now = datetime(2026, 10, 21, tzinfo=timezone.utc)
    result = report(conn, days=90, now=now)
    assert conn.args == (now - timedelta(days=90),)
    assert result['deletion_enabled'] is False
    assert result['groups'][0]['records'] == 2
    assert result['age_basis'] == 'review_updated_at'
    assert conn.query.lstrip().startswith('SELECT')
    assert 'body_text' not in result['groups'][0]


@pytest.mark.parametrize('days', [0, -1, 36501])
def test_invalid_days_do_not_query(days):
    with pytest.raises(ValueError):
        report(Connection(), days=days, now=datetime.now(timezone.utc))
