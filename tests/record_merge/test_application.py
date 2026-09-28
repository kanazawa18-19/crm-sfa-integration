from contextlib import nullcontext
import pytest
from src.record_merge.application import MergeService, validate_plan
from src.record_merge.domain import MergeHeld


class Journal:
    def __init__(self, steps):
        self.job = {'state': 'approved', 'steps': steps}; self.states = {}; self.aliases = False
    def get_authorized(self, *args): return self.job
    def step_state(self, op, index): return self.states.get(index)
    def reserve_step(self, op, index, actor): self.states[index] = 'reserved'
    def complete_step(self, op, index, actor, receipt): self.states[index] = 'done'
    def finish_with_aliases(self, *args): self.aliases = True; self.job['state'] = 'done'
    def hold(self, *args): self.job['state'] = 'held'


class Gateway:
    value = 'before'; calls = 0; fail_after_write = False
    def verify_identity(self, job): pass
    def read(self, step): return self.value
    def write(self, step):
        self.calls += 1; self.value = step['desired']
        if self.fail_after_write: raise TimeoutError('private')
    def verify_completed(self, step): assert self.value == step['desired']
    def verify_archive_ready(self, job): pass


def test_unknown_idempotent_write_recovers_by_readback_without_second_write():
    journal = Journal([{'kind': 'properties', 'before': 'before', 'desired': 'after'}])
    gateway = Gateway(); gateway.fail_after_write = True
    service = MergeService(journal, gateway, lambda job: nullcontext())
    with pytest.raises(MergeHeld, match='外部処理'):
        service.execute('op', 'manager')
    assert not journal.aliases
    gateway.fail_after_write = False
    assert service.execute('op', 'manager')['state'] == 'done'
    assert gateway.calls == 1 and journal.aliases


def test_unknown_body_append_is_not_repeated():
    journal = Journal([{'kind': 'append_body'}]); journal.states[0] = 'reserved'
    with pytest.raises(MergeHeld, match='二重追加'):
        MergeService(journal, Gateway(), lambda job: nullcontext()).execute('op', 'manager')
    assert not journal.aliases


def test_disabled_recovery_only_finalizes_already_applied_steps():
    journal = Journal([{'kind': 'archive', 'before': False, 'desired': True}])
    gateway = Gateway(); gateway.value = True
    service = MergeService(journal, gateway, lambda job: nullcontext())
    assert service.execute('op', 'manager', read_only=True)['state'] == 'done'
    assert gateway.calls == 0 and journal.aliases


def test_disabled_recovery_never_applies_unfinished_steps():
    journal = Journal([{'kind': 'archive', 'before': False, 'desired': True}])
    gateway = Gateway(); gateway.value = False
    with pytest.raises(MergeHeld):
        MergeService(journal, gateway, lambda job: nullcontext()).execute('op', 'manager', read_only=True)
    assert gateway.calls == 0 and not journal.aliases and journal.states == {}


def test_plan_cannot_archive_target_or_write_unseen_page():
    snapshot = {'sourceId': 's', 'targetId': 't', 'dbKey': 'project', 'children': []}
    with pytest.raises(MergeHeld, match='統合元'):
        validate_plan(snapshot, [{'kind': 'archive', 'id': 't', 'dbKey': 'project'}])
    with pytest.raises(MergeHeld, match='比較していない'):
        validate_plan(snapshot, [{'kind': 'properties', 'id': 'other', 'dbKey': 'project'},
                                  {'kind': 'archive', 'id': 's', 'dbKey': 'project'}])
