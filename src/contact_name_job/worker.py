"""既存の保存前後照合を維持して、短い単位で処理する。"""
import os
import time

from src.audit_log.actor_context import set_actor
from src.contact_name_job.domain import decision, matches
from src.contact_name_job.repository import Repository, record_lock
from src.db_schema.registry import get_schema
from src.email_review.gateway import Gateway
from src.sync_engine.clients.notion_client import HttpNotionClient


class NameOnlyClient(HttpNotionClient):
    before_write = None

    def _request(self, method, path, **kwargs):
        if method == 'PATCH' and self.before_write is not None:
            self.before_write()
        return super()._request(method, path, **kwargs)

    def get_raw_page_with_relations(self, page_id, property_names):
        # 姓名しか判断しない。大量の関連レコードを展開しない。
        return self.get_raw_page(page_id)


class JobGateway(Gateway):
    def __init__(self, shard):
        # 通常同期用の接続1は使わない。未設定なら書き込まず失敗する。
        self.api_key = os.environ[f'NOTION_API_KEY_{shard + 2}']

    def client(self, key):
        return NameOnlyClient(key, get_schema(key).notion_database_id, api_key=self.api_key,
                                timeout=15, max_retries=0, max_rate_limit_retries=0)


def complete_one(entry, gateway, check_owner=lambda: None):
    with set_actor('manual', '2026-10-18本人承認：GASから既存連絡先の姓名補完'):
        with record_lock('contact', entry['id']) as check_record:
            state = decision(entry, gateway.live('contact', entry['id']))
            check_record()
            check_owner()
            if state != 'update':
                return state
            client = gateway.client('contact')
            if isinstance(client, NameOnlyClient):
                client.before_write = lambda: (check_record(), check_owner())
            client.update_page(entry['id'], entry['changes'])
            if not matches(gateway.live('contact', entry['id']), {**entry['before'], **entry['changes']}):
                raise ValueError('保存後の照合不一致')
            check_record()
            check_owner()
            return 'verified'


def run_shard(shard, *, repository=None, gateway=None, clock=time.monotonic, budget=25):
    repository = repository or Repository()
    result = {'shard': shard, 'verified': 0, 'held': 0, 'retry': 0}
    with repository.shard(shard) as conn:
        if conn is None:
            return {**result, 'status': 'busy'}
        if not repository.active(conn):
            return {**result, 'status': 'paused'}
        gateway = gateway or JobGateway(shard)
        repository.started(conn)
        started = clock()
        while clock() - started < budget:
            if not repository.active(conn):
                break
            row = repository.next(conn, shard)
            if not row:
                break
            entry = row['plan']
            try:
                state = complete_one(entry, gateway, lambda: conn.execute('SELECT 1'))
            except Exception as exc:
                repository.finish(conn, entry, 'retry', type(exc).__name__,
                                  count_attempt=getattr(exc, 'status_code', None) != 429)
                result['retry'] += 1
                break  # 接続障害で残件すべてを失敗扱いにしない。
            repository.finish(conn, entry, state)
            result[state] += 1
    return {**result, 'status': 'retry' if result['retry'] else 'ok'}
