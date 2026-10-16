"""外部書込みは代替し、応答紛失時の二重作成防止と同一記録の再配送を検証。"""
from copy import deepcopy
from src.email_review.service import Service
from src.email_review.domain import Conflict


class Repo:
    def checkpoint(self, row, **changes):
        self.row = {**row, **changes, 'revision': row['revision'] + 1}
        return deepcopy(self.row)
    finish = checkpoint


class Gateway:
    def __init__(self):
        self.created = self.updated = 0
        self.fail_create = self.fail_delivery = False
    def validate(self, row, selection):
        return {**selection, 'company_name': '会社', 'contact_name': '相手', 'target_name': '施設', 'project_name': None}
    def actor(self, owner):
        return 'notion-user'
    def link_contact(self, row):
        return {'取引先マスター': ['company']}, True
    def action_properties(self, row):
        return {'title': 'メール共通ID'}
    def create_action(self, properties):
        self.created += 1
        if self.fail_create:
            raise TimeoutError('外部応答不明')
        return 'action-id'
    def update_action(self, row, properties):
        self.updated += 1
    def dispatch(self, *args):
        if self.fail_delivery:
            raise TimeoutError()
        return 'delivered'
    def find_action(self, row):
        return 'action-id'


def test_create_timeout_reconcile_then_updates_existing_action_only():
    repo, gateway = Repo(), Gateway()
    service = Service(repo, gateway)
    gateway.fail_create = True
    result = service.process({'id': 'r', 'revision': 0, 'owner': 'me@example.test', 'contact_id': 'c', 'company_id': 'company'})
    assert result['state'] == 'unknown'
    assert gateway.created == 1
    gateway.fail_create = False
    recovered = service.reconcile(result)
    assert recovered['action_id'] == 'action-id'
    result = service.process(recovered)
    assert result['delivery'] == 'delivered'
    assert gateway.created == 1 and gateway.updated == 1


def test_delivery_failure_preserves_action_and_retry_does_not_create():
    repo, gateway = Repo(), Gateway()
    service = Service(repo, gateway)
    gateway.fail_delivery = True
    result = service.process({'id': 'r', 'revision': 0, 'owner': 'me@example.test', 'contact_id': 'c', 'company_id': 'company'})
    assert result['state'] == 'done' and result['delivery'] == 'partial'
    assert result['action_id'] == 'action-id'
    gateway.fail_delivery = False
    assert service.process(result)['delivery'] == 'delivered'
    assert gateway.created == 1


def test_conflicting_affiliation_stops_before_any_write():
    repo, gateway = Repo(), Gateway()
    gateway.validate = lambda *args: (_ for _ in ()).throw(Conflict('所属が変化'))
    result = Service(repo, gateway).process({'id': 'r', 'revision': 0})
    assert result['state'] == 'pending'
    assert gateway.created == gateway.updated == 0
