"""全件収集が終わるまで送らず、送達不明の処理を自動再送しない。"""
import hashlib
import time
from src.reports.durable.domain import ReportHeld, accept_page, query_body, delivery_action


class ReportRunner:
    def __init__(self, journal, clients, render, send, *, clock=time.monotonic):
        self.journal, self.clients, self.render, self.send, self.clock = journal, clients, render, send, clock

    def run(self, job, destination, *, collection_budget=120, total_budget=230):
        started = self.clock(); report_date = job['reportDate']
        try:
            while job['phase'] in {'project', 'action'} and self.clock() - started < collection_budget:
                response = self.clients[job['phase']].query_raw(query_body(job['cursor'], job['cutoff'].isoformat()))
                cursor, complete = accept_page(job['cursor'], response)
                self.journal.save_page(job, response['results'], cursor, complete)
                job = self.journal.get(report_date)
            if job['phase'] != 'ready':
                return {'date': report_date.isoformat(), 'state': 'collecting', 'phase': job['phase'], 'daily_report_sent': False}
            if not destination:
                self.journal.error(report_date, '配信先が未設定です。送信していません')
                return {'date': report_date.isoformat(), 'state': 'not_configured', 'daily_report_sent': False}
            destination_hash = hashlib.sha256(destination.encode()).hexdigest()
            kinds = ['daily', 'weekly'] if report_date.weekday() == 4 else ['daily']
            for kind in kinds:
                existing = self.journal.delivery(report_date, kind)
                if delivery_action(existing, destination_hash) == 'skip': continue
                if self.clock() - started > total_budget - 60:
                    return {'date': report_date.isoformat(), 'state': 'ready', 'daily_report_sent': bool(self.journal.delivery(report_date, 'daily'))}
                text = self.render(report_date, kind, self.journal)
                if self.clock() - started > total_budget - 15:
                    return {'date': report_date.isoformat(), 'state': 'ready', 'daily_report_sent': False}
                # 予約が保存された後は、タイムアウトもDB保存失敗も自動再送しない。
                self.journal.reserve(report_date, kind, destination_hash, hashlib.sha256(text.encode()).hexdigest())
                self.send(destination, text)
                self.journal.delivered(report_date, kind)
            self.journal.finish(report_date)
            return {'date': report_date.isoformat(), 'state': 'done', 'daily_report_sent': True, 'weekly_report_sent': 'weekly' in kinds}
        except ReportHeld as exc:
            self.journal.error(report_date, str(exc), held=True)
            return {'date': report_date.isoformat(), 'state': 'held', 'reason': str(exc), 'daily_report_sent': False}
        except Exception:
            # APIエラーにURL・本文が含まれてもログ/DBへ保存しない。
            self.journal.error(report_date, '収集または送達の確認が必要です。3回続けて失敗した場合は保留し、他の日の処理を進めます')
            latest = self.journal.get(report_date)
            return {'date': report_date.isoformat(), 'state': 'held' if latest['phase'] == 'held' else 'retry_pending', 'daily_report_sent': False}
