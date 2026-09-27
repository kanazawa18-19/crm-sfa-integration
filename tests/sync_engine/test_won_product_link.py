"""受注関連の追加、途中失敗の再処理、共通ロックを検証する（外部通信なし）。"""
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from src.db_schema.base import Tool
from src.db_schema.project import CONFIRMED_STATUSES, ACTIVE_STATUSES
from src.sync_engine.dispatcher import Dispatcher, PropertyDispatchResult
from src.sync_engine.id_mapping import IdMapping, SQLiteIdMappingStore
from src.sync_engine.record_sync_lock import acquire_record_sync_lock, RecordSyncBusy
from src.sync_engine.sync_event import SyncEvent
from src.sync_engine.won_product_link import WonProductLinker, ProductLinkIssue, ProductLinkPersistenceError
from src.sync_engine.won_product_link_queue import drain_project_product_links
from tests.sync_engine.test_dispatcher import FakeSyncTarget

NOW = datetime(2026, 9, 28, tzinfo=timezone.utc)


class Queue:
    def __init__(self):
        self.ids = set()
        self.errors = []
        self.tasks, self.work, self.delivery = {}, {}, {}
        self.expected, self.verification = {}, {}

    def enqueue(self, key):
        self.ids.add(key)
        self.tasks.setdefault(key, {"evaluationHeld": False})
        self.work.setdefault(key, {})
        self.delivery.setdefault(key, {})

    def get(self, key):
        return self.tasks.get(key)

    def plan(self, key, pairs):
        self.work[key] = {pair: self.work[key].get(pair, 'pending') for pair in pairs}
        for (db, notion), state in self.delivery[key].items():
            if state == 'held':
                self.hold_delivery(key, db, notion, 'DELIVERY_NOT_VISIBLE')

    def pairs(self, key, limit):
        return sorted(pair for pair, state in self.work[key].items() if state == 'pending')[:limit]

    def require_delivery(self, key, product, client):
        self.delivery[key].setdefault(('product', product), 'pending')
        self.delivery[key].setdefault(('client_master', client), 'pending')
        self.expected.setdefault((key, 'product', product), set()).add(client)
        self.expected.setdefault((key, 'client_master', client), set()).add(product)

    def expected_ids(self, key, db, notion):
        return self.expected[key, db, notion]

    def unconfirmed(self, key, db, notion):
        target = key, db, notion
        self.verification[target] = self.verification.get(target, 0) + 1
        return self.verification[target]

    def finish_pair(self, key, product, client):
        self.work[key][product, client] = 'done'

    def hold_pair(self, key, product, client, *error):
        self.work[key][product, client] = 'held'

    def deliveries(self, key, limit):
        return sorted(pair for pair, state in self.delivery[key].items() if state == 'pending')[:limit]

    def finish_delivery(self, key, db, notion):
        del self.delivery[key][db, notion]
        self.expected.pop((key, db, notion), None)
        self.verification.pop((key, db, notion), None)

    def hold_delivery(self, key, db, notion, error):
        self.delivery[key][db, notion] = 'held'
        for pair, state in self.work[key].items():
            if state == 'pending' and ((db == 'product' and pair[0] == notion) or (db == 'client_master' and pair[1] == notion)):
                self.work[key][pair] = 'held'

    def complete(self, key):
        if self.tasks[key]['evaluationHeld'] or any(state != 'done' for state in self.work[key].values()) or self.delivery[key]:
            return False
        self.ids.remove(key)
        del self.tasks[key], self.work[key], self.delivery[key]
        return True

    def defer(self, key):
        self.tasks[key]['deferred'] = True

    def fail(self, key, error, **details):
        self.errors.append(error)
        self.tasks[key].update(lastError=error, errorDbKey=details.get('db_key'), errorNotionId=details.get('notion_id'))
        if details.get('permanent') and details.get('evaluation'):
            self.tasks[key]['evaluationHeld'] = True
            self.work[key] = {pair: 'held' if state == 'pending' else state for pair, state in self.work[key].items()}

    def resume(self, key):
        self.tasks[key]['evaluationHeld'] = False
        self.work[key] = {pair: 'pending' if state == 'held' else state for pair, state in self.work[key].items()}
        self.delivery[key] = {pair: 'pending' for pair in self.delivery[key]}
        self.verification = {target: 0 if target[0] == key else attempts for target, attempts in self.verification.items()}

    def pending(self, limit):
        return sorted(key for key in self.ids if (
            (not self.tasks[key]['evaluationHeld'] and 'held' not in self.work[key].values() and 'held' not in self.delivery[key].values())
            or 'pending' in self.work[key].values() or 'pending' in self.delivery[key].values()
        ))[:limit]

    def contains(self, key):
        return key in self.ids


class Client:
    def __init__(self, db, pages):
        self.db, self.pages = db, pages
        self.writes = []

    def get_raw_page(self, key):
        values = self.pages[key]
        properties = {
            name: ({"type": "relation", "has_more": False, "relation": [{"id": v} for v in value]}
                   if isinstance(value, list) else {"type": "select", "select": {"name": value}})
            for name, value in values.items()
        }
        return {"id": key, "properties": properties}

    def get_raw_page_with_relations(self, key, names):
        return self.get_raw_page(key)

    def update_page(self, key, values):
        self.writes.append((key, deepcopy(values)))
        old = self.pages[key]["サービス・商品"]
        self.pages[key].update(deepcopy(values))
        for product in values["サービス・商品"]:
            if product not in old:
                self.pages[product]["取引先マスター"].append(key)


@pytest.fixture
def setup():
    pages = {
        "project": {"営業ステータス": "口頭受注", "サービス・商品": ["product"], "取引先マスター": ["client", "client"]},
        "product": {"取引先マスター": ["existing"]},
        "client": {"サービス・商品": ["other"]},
    }
    store = SQLiteIdMappingStore()
    for key, db in [("project", "project"), ("product", "product"), ("client", "client_master")]:
        store.upsert(IdMapping(notion_key=key, db_key=db))
    clients = {db: Client(db, pages) for db in ("project", "product", "client_master")}
    propagated = []
    def propagate(mapping, prop, values, record):
        propagated.append((mapping.db_key, mapping.notion_key, prop, list(values)))
        return PropertyDispatchResult(prop, None, frozenset({Tool.NOTION, Tool.SPREADSHEET}), frozenset({Tool.ZOHO, Tool.KINTONE}))
    queue = Queue()
    linker = WonProductLinker(store, clients, propagate, queue)
    return pages, store, clients, propagated, linker


@pytest.mark.parametrize("status", sorted(CONFIRMED_STATUSES | {"口頭受注"}))
def test_links_all_confirmed_and_verbal_without_reclassifying_analytics(setup, status):
    pages, store, clients, propagated, linker = setup
    pages["project"]["営業ステータス"] = status
    linker(store.get("project"))
    linker(store.get("project"))
    assert pages["product"]["取引先マスター"] == ["existing", "client"]
    assert pages["client"]["サービス・商品"] == ["other", "product"]
    assert len(clients["client_master"].writes) == 1
    assert len(propagated) == 4  # 再送でも送り先の失敗を取り返す。
    assert "口頭受注" in ACTIVE_STATUSES
    assert "口頭受注" not in CONFIRMED_STATUSES
    assert not linker.queue.ids


@pytest.mark.parametrize("status", ["失注", "解約", "", "商談中"])
def test_does_not_remove_links_when_status_is_not_won(setup, status):
    pages, store, clients, propagated, linker = setup
    linker(store.get("project"))
    pages["project"]["営業ステータス"] = status
    linker(store.get("project"))
    assert pages["product"]["取引先マスター"] == ["existing", "client"]
    assert len(propagated) == 2


def test_later_assignment_and_multiple_clients_products(setup):
    pages, store, clients, propagated, linker = setup
    pages["project"]["サービス・商品"] = []
    linker(store.get("project"))
    assert not propagated
    pages["project"]["サービス・商品"] = ["product", "product2"]
    pages["project"]["取引先マスター"].append("client2")
    pages["product2"] = {"取引先マスター": []}
    pages["client2"] = {"サービス・商品": []}
    store.upsert(IdMapping(notion_key="product2", db_key="product"))
    store.upsert(IdMapping(notion_key="client2", db_key="client_master"))
    linker(store.get("project"))
    assert pages["product"]["取引先マスター"] == ["existing", "client", "client2"]
    assert pages["product2"]["取引先マスター"] == ["client", "client2"]


def test_more_than_100_clients_are_preserved_via_inverse_relation(setup):
    pages, store, clients, propagated, linker = setup
    pages["product"]["取引先マスター"] = [f"existing{i}" for i in range(130)]
    linker(store.get("project"))
    assert len(pages["product"]["取引先マスター"]) == 131
    assert not clients["product"].writes
    assert len(propagated[-1][-1]) == 131


def test_inverse_limit_fails_without_deleting_or_clearing_task(setup):
    pages, store, clients, propagated, linker = setup
    pages["client"]["サービス・商品"] = [f"p{i}" for i in range(100)]
    with pytest.raises(RuntimeError, match="100"):
        linker(store.get("project"))
    assert not clients["client_master"].writes
    assert linker.queue.ids == {"project"}


def test_sheet_failure_after_notion_update_is_retried_from_latest(setup):
    pages, store, clients, propagated, linker = setup
    original = linker._propagate
    linker._propagate = lambda *args: (_ for _ in ()).throw(RuntimeError("sheet failed"))
    with pytest.raises(RuntimeError):
        linker(store.get("project"))
    assert pages["product"]["取引先マスター"] == ["existing", "client"]
    assert linker.queue.ids == {"project"}
    linker._propagate = original
    result = drain_project_product_links(store, linker)
    assert result == {"completed": 1, "failed": 0, "busy": 0, "no_delivery_this_run": 0, "unsupported_relations": 2, "deferred": 0}
    assert len(clients["client_master"].writes) == 1
    assert not linker.queue.ids
    assert propagated[-1][-1] == ["existing", "client"]


def test_uses_product_lock_shared_with_normal_sync(setup):
    pages, store, clients, propagated, linker = setup
    with acquire_record_sync_lock(store, "product", "product"):
        with pytest.raises(ProductLinkIssue):
            linker(store.get("project"))
    assert not propagated
    assert linker.queue.ids == {"project"}


def test_cron_and_webhook_share_project_lock(setup):
    pages, store, clients, propagated, linker = setup
    linker.queue.enqueue("project")
    with acquire_record_sync_lock(store, "project", "project"):
        assert drain_project_product_links(store, linker)["busy"] == 1
    assert linker.queue.ids == {"project"}
    assert not propagated
    assert drain_project_product_links(store, linker)["completed"] == 1


def test_dispatch_failure_leaves_watermark_retryable(setup):
    pages, store, clients, propagated, linker = setup
    fail = [True]
    def hook(mapping):
        if fail[0]:
            raise RuntimeError("retry")
        return linker(mapping)
    dispatcher = Dispatcher(store, {}, project_linker=hook)
    event = SyncEvent(Tool.NOTION, "project", "project", NOW, {})
    with pytest.raises(RuntimeError):
        dispatcher.dispatch(event)
    assert store.get("project").last_synced_at is None
    fail[0] = False
    assert not dispatcher.dispatch(event).skipped
    assert store.get("project").last_synced_at == NOW
    assert dispatcher.dispatch(event).reason == "stale_event"


@pytest.mark.parametrize("source", [Tool.NOTION, Tool.ZOHO, Tool.KINTONE, Tool.SPREADSHEET])
def test_each_source_calls_linker_after_normal_dispatch(source):
    store = SQLiteIdMappingStore()
    store.upsert(IdMapping(notion_key="project", db_key="project", zoho_id="z", kintone_id="k", spreadsheet_row=1))
    seen = []
    dispatcher = Dispatcher(store, {}, project_linker=lambda mapping: seen.append(mapping.notion_key) or ())
    external = {Tool.NOTION: "project", Tool.ZOHO: "z", Tool.KINTONE: "k", Tool.SPREADSHEET: "1"}[source]
    result = dispatcher.dispatch(SyncEvent(source, "project", external, NOW, {}, source_notion_key="project" if source == Tool.SPREADSHEET else None))
    assert not result.skipped
    assert seen == ["project"]


def test_unsupported_external_relations_are_not_success():
    store = SQLiteIdMappingStore()
    mapping = IdMapping(notion_key="product", db_key="product", spreadsheet_row=1)
    store.upsert(mapping)
    dispatcher = Dispatcher(store, {Tool.SPREADSHEET: FakeSyncTarget(Tool.SPREADSHEET)})
    result = dispatcher.propagate_linked_relation(mapping, "取引先マスター", ["client"], {})
    assert result.skipped_tools == {Tool.ZOHO, Tool.KINTONE}
    assert result.written_tools == {Tool.NOTION, Tool.SPREADSHEET}
    assert result.related_db_key == "product"


def test_external_creation_link_failure_does_not_mark_event_complete(monkeypatch):
    monkeypatch.setenv("AUTO_CREATE_NEW_RECORDS_ENABLED", "true")
    monkeypatch.setattr("src.sync_engine.dispatcher.build_notion_properties_for_new_record", lambda *args, **kwargs: {"案件名": "検証", "営業ステータス": "口頭受注"})
    store = SQLiteIdMappingStore()
    notion = FakeSyncTarget(Tool.NOTION)
    source = FakeSyncTarget(Tool.KINTONE, {"external": {"title": "検証"}})
    failure = [True]
    seen = []
    def hook(mapping):
        seen.append(mapping.notion_key)
        if failure[0]:
            raise RuntimeError("link retry")
        return ()
    dispatcher = Dispatcher(store, {Tool.NOTION: notion, Tool.KINTONE: source}, project_linker=hook)
    event = SyncEvent(Tool.KINTONE, "project", "external", NOW, {})
    with pytest.raises(RuntimeError, match="link retry"):
        dispatcher.dispatch(event)
    assert store.get("new-id").last_synced_at is None
    failure[0] = False
    assert not dispatcher.dispatch(event).skipped
    assert len(notion.upsert_calls) == 1
    assert seen == ["new-id", "new-id"]
    assert store.get("new-id").last_synced_at == NOW


def test_sheet_creation_routes_to_linker_and_failure_can_be_replayed():
    from types import SimpleNamespace
    from src.sync_engine.production_wiring import SkipTrackingDispatcher
    store = SQLiteIdMappingStore()
    store.upsert(IdMapping(notion_key="new-project", db_key="project"))
    calls = []
    def hook(mapping):
        calls.append(mapping.notion_key)
        if len(calls) == 1:
            raise RuntimeError("retry")
        return ()
    dispatcher = Dispatcher(store, {}, project_linker=hook)
    service = SimpleNamespace(
        handle=lambda event: "hub_creation_complete",
        journal=SimpleNamespace(get=lambda source, target: {"state": "created", "externalId": "new-project"}),
    )
    wrapper = SkipTrackingDispatcher(dispatcher, creation_service=service)
    event = SyncEvent(Tool.SPREADSHEET, "project", "1", NOW, {}, registration_key="new:test")
    with pytest.raises(RuntimeError):
        wrapper.dispatch(event)
    assert not wrapper.dispatch(event).skipped
    assert calls == ["new-project", "new-project"]


def test_linker_does_not_run_if_external_value_was_not_written_to_notion():
    store = SQLiteIdMappingStore()
    store.upsert(IdMapping(notion_key="project", db_key="project", kintone_id="external"))
    calls = []
    dispatcher = Dispatcher(
        store, {Tool.NOTION: FakeSyncTarget(Tool.NOTION, always_skip=True)},
        project_linker=lambda mapping: calls.append(mapping.notion_key) or (),
    )
    result = dispatcher.dispatch(SyncEvent(Tool.KINTONE, "project", "external", NOW, {"営業ステータス": "口頭受注"}))
    assert result.has_partial_skips
    assert calls == []
    assert store.get("project").last_synced_at == NOW


def test_queue_failure_happens_before_any_related_write(setup):
    pages, store, clients, propagated, linker = setup
    linker.queue.enqueue = lambda key: (_ for _ in ()).throw(RuntimeError("queue unavailable"))
    with pytest.raises(ProductLinkPersistenceError):
        linker(store.get("project"))
    assert not clients["client_master"].writes
    assert not propagated


@pytest.mark.parametrize('change', ['lost', 'empty', 'replacement'])
def test_already_changed_targets_are_delivered_even_if_project_changes(setup, change):
    pages, store, clients, propagated, linker = setup
    original = linker._propagate
    linker._propagate = lambda *args: (_ for _ in ()).throw(RuntimeError('sheet unavailable'))
    with pytest.raises(ProductLinkIssue, match='DELIVERY_FAILED'):
        linker(store.get('project'))
    assert pages['client']['サービス・商品'] == ['other', 'product']
    assert set(linker.queue.delivery['project']) == {('client_master', 'client'), ('product', 'product')}
    if change == 'lost':
        pages['project']['営業ステータス'] = '失注'
    elif change == 'empty':
        pages['project']['サービス・商品'] = []
        pages['project']['取引先マスター'] = []
    else:
        pages['project']['サービス・商品'] = ['new-product']
        pages['new-product'] = {'取引先マスター': []}
        store.upsert(IdMapping(notion_key='new-product', db_key='product'))
    # 配送義務には古い値を保存しない。再処理時の他案件による関連追加も保持する。
    pages['product']['取引先マスター'].append('added-later')
    pages['client']['サービス・商品'].append('service-added-later')
    linker._propagate = original
    result = drain_project_product_links(store, linker)
    assert result['completed'] == 1
    assert not linker.queue.ids
    old_product = [row for row in propagated if row[1] == 'product']
    assert old_product[-1][-1] == ['existing', 'client', 'added-later']
    old_client = [row for row in propagated if row[1] == 'client']
    assert 'service-added-later' in old_client[-1][-1]


def test_old_pending_pair_is_not_applied_after_project_is_lost(setup):
    pages, store, clients, propagated, linker = setup
    linker._pair_limit = 0
    linker(store.get('project'))
    assert linker.queue.pairs('project', 5) == [('product', 'client')]
    assert not clients['client_master'].writes
    pages['project']['営業ステータス'] = '失注'
    linker._pair_limit = 5
    linker(store.get('project'))
    assert not clients['client_master'].writes
    assert not linker.queue.ids


def test_pair_and_delivery_progress_survive_time_budget_boundary(setup):
    pages, store, clients, propagated, linker = setup
    pages['project']['サービス・商品'].append('product2')
    pages['product2'] = {'取引先マスター': []}
    store.upsert(IdMapping(notion_key='product2', db_key='product'))
    clock = [0.0]
    linker._clock = lambda: clock[0]
    original_write = clients['client_master'].update_page
    def slow_write(*args):
        original_write(*args)
        clock[0] += 21  # 1つ目の原子的な関連追加完了時に予算を超える。
    clients['client_master'].update_page = slow_write
    linker(store.get('project'))
    assert linker.queue.work['project'][('product', 'client')] == 'done'
    assert linker.queue.work['project'][('product2', 'client')] == 'pending'
    assert not propagated
    assert linker.queue.delivery['project']
    # 第2回は先頭の完了ペアを再実行しない。
    linker(store.get('project'))
    assert len(clients['client_master'].writes) == 2
    assert linker.queue.work['project'][('product2', 'client')] == 'done'
    # 第3回は配送段階から再開し、進捗完了後のみ親タスクが消える。
    linker(store.get('project'))
    assert len(clients['client_master'].writes) == 2
    assert not linker.queue.ids
    assert len(propagated) == 3


def test_delivery_limit_consumes_items_without_restarting_from_first(setup):
    pages, store, clients, propagated, linker = setup
    linker._delivery_limit = 1
    linker(store.get('project'))
    assert propagated[0][0] == 'client_master'
    assert linker.queue.delivery['project'] == {('product', 'product'): 'pending'}
    linker(store.get('project'))
    assert [row[0] for row in propagated] == ['client_master', 'product']
    assert not linker.queue.ids


@pytest.mark.parametrize('bad_prop', [None, {'type': 'relation', 'has_more': False, 'relation': None},
    {'type': 'relation', 'has_more': False}, {'type': 'rich_text', 'has_more': False, 'relation': []},
    {'type': 'relation', 'relation': []}, {'type': 'relation', 'has_more': 'false', 'relation': []},
    {'type': 'relation', 'has_more': True, 'relation': []},
    {'type': 'relation', 'has_more': False, 'relation': [{'id': ''}]},
    {'type': 'relation', 'has_more': False, 'relation': [{'id': None}]},
    {'type': 'relation', 'has_more': False, 'relation': [{'id': ' '}]},
])
def test_malformed_client_relation_never_replaces_existing_links(setup, bad_prop):
    pages, store, clients, propagated, linker = setup
    original = clients['client_master'].get_raw_page_with_relations
    def invalid(key, names):
        raw = original(key, names)
        if bad_prop is None:
            del raw['properties']['サービス・商品']
        else:
            raw['properties']['サービス・商品'] = bad_prop
        return raw
    clients['client_master'].get_raw_page_with_relations = invalid
    with pytest.raises(ProductLinkIssue, match='INVALID_RELATION'):
        linker(store.get('project'))
    assert pages['client']['サービス・商品'] == ['other']
    assert not clients['client_master'].writes
    assert not propagated
    assert linker.queue.ids == {'project'}
    assert linker.queue.tasks['project']['errorDbKey'] == 'client_master'
    assert linker.queue.tasks['project']['errorNotionId'] == 'client'
    assert linker.queue.pending(3) == []  # 恒久保留を毎時無限再試行しない。
    clients['client_master'].get_raw_page_with_relations = original
    linker.queue.resume('project')
    assert drain_project_product_links(store, linker)['completed'] == 1


def test_malformed_delivery_relation_is_held_even_after_project_lost(setup):
    pages, store, clients, propagated, linker = setup
    linker._delivery_limit = 0
    linker(store.get('project'))
    pages['project']['営業ステータス'] = '失注'
    original = clients['client_master'].get_raw_page_with_relations
    def invalid(key, names):
        raw = original(key, names)
        raw['properties']['サービス・商品']['relation'] = None
        return raw
    clients['client_master'].get_raw_page_with_relations = invalid
    linker._delivery_limit = 10
    with pytest.raises(ProductLinkIssue, match='INVALID_RELATION'):
        linker(store.get('project'))
    assert linker.queue.delivery['project'] == {('client_master', 'client'): 'held'}
    assert [row[0] for row in propagated] == ['product']
    assert linker.queue.pending(3) == []


def test_archived_project_does_not_cancel_existing_delivery(setup):
    pages, store, clients, propagated, linker = setup
    linker._delivery_limit = 0
    linker(store.get('project'))
    original = clients['project'].get_raw_page
    clients['project'].get_raw_page = lambda key: {**original(key), 'archived': True}
    linker._delivery_limit = 10
    with pytest.raises(ProductLinkIssue, match='PAGE_ARCHIVED'):
        linker(store.get('project'))
    assert len(propagated) == 2
    assert linker.queue.ids == {'project'}
    assert not linker.queue.delivery['project']
    assert linker.queue.pending(3) == []


def test_unmapped_external_relation_does_not_fetch_external_versions():
    store = SQLiteIdMappingStore()
    mapping = IdMapping(notion_key='product', db_key='product', zoho_id='z', kintone_id='k', spreadsheet_row=1)
    store.upsert(mapping)
    zoho, kintone = FakeSyncTarget(Tool.ZOHO), FakeSyncTarget(Tool.KINTONE)
    dispatcher = Dispatcher(store, {Tool.SPREADSHEET: FakeSyncTarget(Tool.SPREADSHEET), Tool.ZOHO: zoho, Tool.KINTONE: kintone})
    result = dispatcher.propagate_linked_relation(mapping, '取引先マスター', ['client'], {})
    assert result.skipped_tools == {Tool.ZOHO, Tool.KINTONE}
    assert not zoho.get_record_calls and not kintone.get_record_calls
    assert not zoho.upsert_calls and not kintone.upsert_calls


@pytest.mark.parametrize('source', [Tool.KINTONE, Tool.ZOHO, Tool.NOTION])
def test_project_is_queued_before_later_sheet_failure(setup, source):
    pages, store, clients, propagated, linker = setup
    store.upsert(IdMapping(notion_key='project', db_key='project', kintone_id='k', zoho_id='z', spreadsheet_row=1))
    notion = FakeSyncTarget(Tool.NOTION)
    sheet = FakeSyncTarget(Tool.SPREADSHEET, upsert_raises=RuntimeError('sheet failed'))
    dispatcher = Dispatcher(store, {Tool.NOTION: notion, Tool.SPREADSHEET: sheet}, project_linker=linker)
    external = {Tool.KINTONE: 'k', Tool.ZOHO: 'z', Tool.NOTION: 'project'}[source]
    with pytest.raises(RuntimeError, match='sheet failed'):
        dispatcher.dispatch(SyncEvent(source, 'project', external, NOW, {'営業ステータス': '口頭受注'}))
    assert linker.queue.ids == {'project'}
    assert len(notion.upsert_calls) == (0 if source == Tool.NOTION else 1)
    assert not clients['client_master'].writes
    assert drain_project_product_links(store, linker)['completed'] == 1


def test_notion_write_failure_does_not_enqueue_link_task(setup):
    pages, store, clients, propagated, linker = setup
    store.upsert(IdMapping(notion_key='project', db_key='project', kintone_id='k', spreadsheet_row=1))
    dispatcher = Dispatcher(store, {Tool.NOTION: FakeSyncTarget(Tool.NOTION, upsert_raises=RuntimeError('notion failed'))}, project_linker=linker)
    with pytest.raises(RuntimeError, match='notion failed'):
        dispatcher.dispatch(SyncEvent(Tool.KINTONE, 'project', 'k', NOW, {'営業ステータス': '口頭受注'}))
    assert not linker.queue.ids


def test_notion_origin_is_queued_before_note_failure(setup):
    pages, store, clients, propagated, linker = setup
    def fail(*args):
        raise RuntimeError('note failed')
    dispatcher = Dispatcher(store, {}, project_linker=linker, note_writer=fail)
    with pytest.raises(RuntimeError, match='note failed'):
        dispatcher.dispatch(SyncEvent(Tool.NOTION, 'project', 'project', NOW, {}, sync_notes={'note': 'value'}))
    assert linker.queue.ids == {'project'}


def test_external_new_project_is_queued_before_notification_failure(monkeypatch, setup):
    pages, store, clients, propagated, linker = setup
    monkeypatch.setenv('AUTO_CREATE_NEW_RECORDS_ENABLED', 'true')
    monkeypatch.setattr('src.sync_engine.dispatcher.build_notion_properties_for_new_record', lambda *args, **kwargs: {'案件名': '検証', '営業ステータス': '口頭受注'})
    class Notifier:
        def notify_new_record_created(self, **kwargs):
            raise RuntimeError('notification failed')
    dispatcher = Dispatcher(store, {
        Tool.NOTION: FakeSyncTarget(Tool.NOTION),
        Tool.KINTONE: FakeSyncTarget(Tool.KINTONE, {'new-external': {'title': '検証'}}),
    }, project_linker=linker, slack_notifier=Notifier())
    with pytest.raises(RuntimeError, match='notification failed'):
        dispatcher.dispatch(SyncEvent(Tool.KINTONE, 'project', 'new-external', NOW, {}))
    assert store.get('new-id') is not None
    assert linker.queue.ids == {'new-id'}


def test_new_sheet_project_queues_before_gateway_or_note_failure(monkeypatch, setup):
    from types import SimpleNamespace
    from src.hub_creation.service import HubCreationService
    from tests.hub_creation.test_service import Journal, Notion
    pages, store, clients, propagated, linker = setup
    monkeypatch.setattr('src.hub_creation.service.sheet_properties', lambda *args: {'案件名': '検証'})
    def fail(*args, **kwargs):
        raise RuntimeError('delivery failed')
    notion = Notion()
    notion.upsert_sync_notes = fail
    service = HubCreationService(
        store=store, journal=Journal(), notion_clients={'project': notion}, adapters=[], enabled_since=NOW,
        sheet_gateway=SimpleNamespace(read=lambda *args, **kwargs: ({}, {}, 1), update=fail),
        project_link_prepare=linker.prepare,
    )
    with pytest.raises(RuntimeError, match='delivery failed'):
        service.handle(SyncEvent(Tool.SPREADSHEET, 'project', '1', NOW, {}, registration_key='new:project'))
    assert notion.creates == 1
    assert store.get('created-page') is not None
    assert linker.queue.ids == {'created-page'}


def test_delayed_inverse_cannot_discard_delivery_then_lose_it_after_lost(setup):
    pages, store, clients, propagated, linker = setup
    original_write = clients['client_master'].update_page
    def delayed_write(key, values):
        original_write(key, values)
        pages['product']['取引先マスター'].remove('client')  # 商品側の反映だけ遅延。
    clients['client_master'].update_page = delayed_write
    with pytest.raises(ProductLinkIssue, match='INVERSE_NOT_VISIBLE'):
        linker(store.get('project'))
    assert [row[0] for row in propagated] == ['client_master']
    assert linker.queue.expected_ids('project', 'product', 'product') == {'client'}
    assert linker.queue.delivery['project'] == {('product', 'product'): 'pending'}
    pages['project']['営業ステータス'] = '失注'
    with pytest.raises(ProductLinkIssue, match='DELIVERY_NOT_VISIBLE'):
        linker(store.get('project'))
    assert not linker.queue.work['project']
    assert linker.queue.ids == {'project'}
    assert [row[0] for row in propagated] == ['client_master']
    pages['product']['取引先マスター'].append('client')
    linker(store.get('project'))
    assert propagated[-1][0] == 'product'
    assert propagated[-1][-1] == ['existing', 'client']
    assert not linker.queue.ids


def test_expected_link_missing_after_write_failure_is_held_after_eight_checks(setup):
    pages, store, clients, propagated, linker = setup
    clients['client_master'].update_page = lambda *args: (_ for _ in ()).throw(RuntimeError('write failed'))
    with pytest.raises(ProductLinkIssue, match='NOTION_WRITE_FAILED'):
        linker(store.get('project'))
    pages['project']['営業ステータス'] = '失注'
    for _ in range(7):
        with pytest.raises(ProductLinkIssue, match='DELIVERY_NOT_VISIBLE'):
            linker(store.get('project'))
    assert set(linker.queue.delivery['project'].values()) == {'held'}
    assert linker.queue.pending(3) == []
    assert not propagated
    assert linker.queue.ids == {'project'}


def test_busy_related_product_does_not_fail_completed_project_dispatch(setup):
    pages, store, clients, propagated, linker = setup
    dispatcher = Dispatcher(store, {}, project_linker=linker)
    with acquire_record_sync_lock(store, 'product', 'product'):
        result = dispatcher.dispatch(SyncEvent(Tool.NOTION, 'project', 'project', NOW, {}))
    assert not result.skipped
    assert store.get('project').last_synced_at == NOW
    assert linker.queue.ids == {'project'}
    assert linker.queue.tasks['project']['lastError'] == 'RECORD_BUSY'


@pytest.mark.parametrize('operation', ['enqueue', 'plan', 'require_delivery', 'finish_pair'])
def test_persistence_failure_is_not_swallowed_by_normal_dispatch(setup, operation):
    pages, store, clients, propagated, linker = setup
    def fail(*args, **kwargs):
        raise RuntimeError('storage unavailable')
    setattr(linker.queue, operation, fail)
    dispatcher = Dispatcher(store, {}, project_linker=linker)
    with pytest.raises(ProductLinkPersistenceError):
        dispatcher.dispatch(SyncEvent(Tool.NOTION, 'project', 'project', NOW, {}))
    assert store.get('project').last_synced_at is None


def test_unrelated_unsupported_notion_property_does_not_fail_project_dispatch():
    from datetime import timedelta
    from src.sync_engine.clients._notion_keys import NOTION_LAST_EDITED_TIME_KEY
    store = SQLiteIdMappingStore()
    store.upsert(IdMapping(notion_key='project', db_key='project', kintone_id='external'))
    class PartialNotion(FakeSyncTarget):
        def unsupported_properties(self, properties, *, db_key=None):
            return frozenset({'テキスト'})
    notion = PartialNotion(Tool.NOTION, {'project': {'テキスト': '大阪', NOTION_LAST_EDITED_TIME_KEY: NOW - timedelta(days=1)}})
    calls = []
    dispatcher = Dispatcher(store, {Tool.NOTION: notion}, project_linker=lambda m: calls.append(m.notion_key) or ())
    result = dispatcher.dispatch(SyncEvent(Tool.KINTONE, 'project', 'external', NOW, {'テキスト': '東京'}))
    assert result.has_partial_skips
    assert calls == ['project']
    assert store.get('project').last_synced_at == NOW


def test_eight_unconfirmed_deliveries_hold_pair_without_blocking_other_delivery(setup):
    pages, store, clients, propagated, linker = setup
    original_write = clients['client_master'].update_page
    def delayed_write(key, values):
        original_write(key, values)
        pages['product']['取引先マスター'].remove('client')
    clients['client_master'].update_page = delayed_write
    for _ in range(8):
        with pytest.raises(ProductLinkIssue):
            linker(store.get('project'))
    assert pages['project']['営業ステータス'] == '口頭受注'
    assert linker.queue.work['project'][('product', 'client')] == 'held'
    assert linker.queue.delivery['project'][('product', 'product')] == 'held'
    assert linker.queue.pending(3) == []
    # 当該案件が更新されても、保留済み対象を自動で再試行しない。
    attempts = linker.queue.verification['project', 'product', 'product']
    linker(store.get('project'))
    assert linker.queue.verification['project', 'product', 'product'] == attempts
    assert len(clients['client_master'].writes) == 1
    # 別対象の発生済み配送義務を持つ場合はそちらを続行できる。
    pages['other-product'] = {'取引先マスター': ['other-client']}
    pages['other-client'] = {'サービス・商品': ['other-product']}
    store.upsert(IdMapping(notion_key='other-product', db_key='product'))
    store.upsert(IdMapping(notion_key='other-client', db_key='client_master'))
    linker.queue.require_delivery('project', 'other-product', 'other-client')
    assert drain_project_product_links(store, linker)['deferred'] == 1
    assert any(row[1] == 'other-product' for row in propagated)
    assert any(row[1] == 'other-client' for row in propagated)
    assert linker.queue.delivery['project'] == {('product', 'product'): 'held'}
    # 原因解消後の明示的resumeではペアと配送の両方が再開する。
    pages['product']['取引先マスター'].append('client')
    linker.queue.resume('project')
    assert linker.queue.work['project'][('product', 'client')] == 'pending'
    assert drain_project_product_links(store, linker)['completed'] == 1
    assert not linker.queue.ids


def test_new_pair_to_held_delivery_is_also_held_until_resume(setup):
    pages, store, clients, propagated, linker = setup
    linker.queue.enqueue('project')
    linker.queue.require_delivery('project', 'product', 'client')
    linker.queue.hold_delivery('project', 'product', 'product', 'DELIVERY_NOT_VISIBLE')
    linker.queue.plan('project', [('product', 'another-client')])
    assert linker.queue.work['project'][('product', 'another-client')] == 'held'
