"""全件収集が終わるまで送らず、送達不明の処理を自動再送しない。"""
import hashlib
import time
import requests
from src.reports.durable.domain import ReportHeld, accept_page, query_body, delivery_action


class ReportRunner:
    def __init__(self, journal, clients, render, send, *, clock=time.monotonic):
        self.journal, self.clients, self.render, self.send, self.clock = journal, clients, render, send, clock

    def run(self, job, destination, *, collection_budget=120, total_budget=230):
        started = self.clock(); report_date = job['reportDate']
        stage = '収集開始'
        try:
            while job['phase'] in {'project', 'action'} and self.clock() - started < collection_budget:
                stage = 'Notion収集'
                response = self.clients[job['phase']].query_raw(query_body(job['cursor'], job['cutoff'].isoformat()))
                stage = '収集応答の検査'
                cursor, complete = accept_page(job['cursor'], response)
                stage = '収集ページの保存'
                self.journal.save_page(job, response['results'], cursor, complete)
                stage = '収集位置の読込'
                job = self.journal.get(report_date)
            if job['phase'] != 'ready':
                return {'date': report_date.isoformat(), 'state': 'collecting', 'phase': job['phase'], 'daily_report_sent': False}
            if not destination:
                self.journal.error(report_date, '配信先が未設定です。送信していません')
                return {'date': report_date.isoformat(), 'state': 'not_configured', 'daily_report_sent': False}
            destination_hash = hashlib.sha256(destination.encode()).hexdigest()
            kinds = ['daily', 'weekly'] if report_date.weekday() == 4 else ['daily']
            for kind in kinds:
                stage = '送達記録の読込'
                existing = self.journal.delivery(report_date, kind)
                if delivery_action(existing, destination_hash) == 'skip': continue
                if self.clock() - started > total_budget - 60:
                    return {'date': report_date.isoformat(), 'state': 'ready', 'daily_report_sent': bool(self.journal.delivery(report_date, 'daily'))}
                stage = '日報の集計'
                text = self.render(report_date, kind, self.journal)
                if self.clock() - started > total_budget - 15:
                    return {'date': report_date.isoformat(), 'state': 'ready', 'daily_report_sent': False}
                # 予約が保存された後は、タイムアウトもDB保存失敗も自動再送しない。
                stage = '送信予約の保存'
                self.journal.reserve(report_date, kind, destination_hash, hashlib.sha256(text.encode()).hexdigest())
                stage = 'Slack送信'
                self.send(destination, text)
                stage = '送達結果の保存'
                self.journal.delivered(report_date, kind)
            stage = '完了状態の保存'
            self.journal.finish(report_date)
            return {'date': report_date.isoformat(), 'state': 'done', 'daily_report_sent': True, 'weekly_report_sent': 'weekly' in kinds}
        except ReportHeld as exc:
            self.journal.error(report_date, str(exc), held=True)
            return {'date': report_date.isoformat(), 'state': 'held', 'reason': str(exc), 'daily_report_sent': False}
        except Exception as exc:
            # APIエラーにURL・本文が含まれてもログ/DBへ保存しない。
            self.journal.error(report_date, safe_failure_reason(stage, exc))
            latest = self.journal.get(report_date)
            return {'date': report_date.isoformat(), 'state': 'held' if latest['phase'] == 'held' else 'retry_pending', 'daily_report_sent': False}


def safe_failure_reason(stage, exc):
    """固定の処理名・分類だけを保存し、例外本文/URL/顧客データを残さない。"""
    category = '処理エラー'
    if isinstance(exc, (TimeoutError, requests.exceptions.Timeout)):
        category = '時間切れ'
    elif isinstance(exc, requests.exceptions.ConnectionError):
        category = '接続失敗'
    else:
        status = getattr(exc, 'status_code', None)
        if type(status) is int and 400 <= status <= 599:
            category = f'HTTP {status}'
        elif getattr(exc, 'sqlstate', None) in {'57014', '40001', '40P01', '08001', '08006'}:
            category = {'57014': 'DB処理時間切れまたは取消', '40001': 'DB更新競合',
                        '40P01': 'DB相互待機', '08001': 'DB接続失敗', '08006': 'DB接続切断'}[exc.sqlstate]
    return f'{stage}で{category}。3回続けて失敗した場合は保留します。送達記録を確認するまで手動再送しないでください'
