"""受注案件の関連追加と、発生済みの配送義務を分けて処理する。"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable

from src.db_schema.project import CONFIRMED_STATUSES
from src.sync_engine.clients.notion_client import NotionRelationDataError, validate_raw_relation
from src.sync_engine.id_mapping import IdMapping, IdMappingStore
from src.sync_engine.record_sync_lock import acquire_record_sync_lock, RecordSyncBusy
from src.sync_engine.webhook_handlers.notion_webhook import parse_notion_property_value, PARSEABLE_NOTION_PROPERTY_TYPES

logger = logging.getLogger(__name__)
LINK_STATUSES = CONFIRMED_STATUSES | {"口頭受注"}
RELATIONS = {"project": {"サービス・商品", "取引先マスター"},
             "product": {"取引先マスター"}, "client_master": {"サービス・商品"}}


class ProductLinkPersistenceError(RuntimeError):
    """関連キューの永続化を確認できないため、通常同期でも握らない。"""


class ProductLinkIssue(RuntimeError):
    def __init__(self, code: str, db_key: str, notion_id: str, *, permanent: bool = True):
        super().__init__(code)
        self.code, self.db_key, self.notion_id, self.permanent = code, db_key, notion_id, permanent


def relation_ids(value: Any) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(v, str) or not v.strip() or v != v.strip() for v in value):
        raise NotionRelationDataError("関連IDの一覧が不正です")
    return list(dict.fromkeys(value))


class WonProductLinker:
    def __init__(self, store: IdMappingStore, clients: dict[str, Any], propagate: Callable, queue,
                 *, pair_limit: int = 5, delivery_limit: int = 10, time_budget: float = 20,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.queue, self._store, self._clients, self._propagate = queue, store, clients, propagate
        self._pair_limit, self._delivery_limit = pair_limit, delivery_limit
        self._time_budget, self._clock = time_budget, clock

    def _queue(self, method: str, *args, **kwargs):
        try:
            return getattr(self.queue, method)(*args, **kwargs)
        except Exception as exc:
            raise ProductLinkPersistenceError("関連キューの保存・読取に失敗しました") from exc

    def _validate_page(self, raw, db_key, page_id):
        if not isinstance(raw, dict) or not isinstance(raw.get('properties'), dict):
            raise ProductLinkIssue('INVALID_PAGE', db_key, page_id)
        if raw.get('archived') or raw.get('in_trash'):
            raise ProductLinkIssue('PAGE_ARCHIVED', db_key, page_id)
        expected = getattr(self._clients[db_key], '_database_id', None)
        actual = raw.get('parent', {}).get('database_id')
        if expected and (not isinstance(actual, str) or actual.replace('-', '') != expected.replace('-', '')):
            raise ProductLinkIssue('DATABASE_MISMATCH', db_key, page_id)

    def _raw(self, db_key: str, page_id: str, *, relations: bool):
        client = self._clients.get(db_key)
        if client is None:
            raise ProductLinkIssue('NOTION_CLIENT_MISSING', db_key, page_id)
        try:
            # get_raw_page_with_relationsで必要な関連だけhas_moreを最後まで取得する。
            raw = (client.get_raw_page_with_relations(page_id, RELATIONS[db_key])
                   if relations else client.get_raw_page(page_id))
        except NotionRelationDataError as exc:
            raise ProductLinkIssue('INVALID_RELATION', db_key, page_id) from exc
        except Exception as exc:
            status = getattr(exc, 'status_code', None)
            raise ProductLinkIssue(
                'PAGE_NOT_FOUND' if status == 404 else 'NOTION_READ_FAILED', db_key, page_id,
                permanent=status in (400, 401, 403, 404),
            ) from exc
        self._validate_page(raw, db_key, page_id)
        return raw

    def _page(self, db_key: str, page_id: str) -> dict[str, Any]:
        raw = self._raw(db_key, page_id, relations=True)
        for name in RELATIONS[db_key]:
            try:
                prop = raw['properties'].get(name)
                validate_raw_relation(prop)
                if prop['has_more']:
                    raise NotionRelationDataError('関連の取得が未完了です')
            except NotionRelationDataError as exc:
                raise ProductLinkIssue('INVALID_RELATION', db_key, page_id) from exc
        return {name: parse_notion_property_value(prop) for name, prop in raw['properties'].items()
                if isinstance(prop, dict) and prop.get('type') in PARSEABLE_NOTION_PROPERTY_TYPES}

    def _mapping(self, db_key: str, page_id: str) -> IdMapping:
        mapping = self._store.get(page_id)
        if mapping is None or mapping.db_key != db_key:
            raise ProductLinkIssue('MAPPING_MISSING', db_key, page_id)
        return mapping

    def _plan(self, project: IdMapping) -> None:
        self._mapping('project', project.notion_key)
        raw = self._raw('project', project.notion_key, relations=False)
        status = raw['properties'].get('営業ステータス')
        if not isinstance(status, dict) or status.get('type') not in ('status', 'select'):
            raise ProductLinkIssue('INVALID_STATUS', 'project', project.notion_key)
        pairs = []
        if parse_notion_property_value(status) in LINK_STATUSES:
            values = self._page('project', project.notion_key)
            if values.get('営業ステータス') in LINK_STATUSES:
                pairs = [(p, c) for p in sorted(relation_ids(values['サービス・商品']))
                         for c in sorted(relation_ids(values['取引先マスター']))]
        self._queue("plan", project.notion_key, pairs)

    def _apply_pair(self, project_id, product_id, client_id):
        # 商品・取引先の通常同期と共通のロック。案件ロックは呼び出し元が保持する。
        with acquire_record_sync_lock(self._store, 'product', product_id):
            self._mapping('product', product_id)
            product = self._page('product', product_id)
            existing = relation_ids(product['取引先マスター'])
            with acquire_record_sync_lock(self._store, 'client_master', client_id):
                self._mapping('client_master', client_id)
                client = self._page('client_master', client_id)
                inverse = relation_ids(client['サービス・商品'])
                if product_id not in inverse and len(inverse) >= 100:
                    raise ProductLinkIssue('RELATION_LIMIT_100', 'client_master', client_id)
                # PATCH前に双方の配送を確定。案件が失注・空欄になっても取り消さない。
                self._queue("require_delivery", project_id, product_id, client_id)
                if product_id not in inverse:
                    inverse.append(product_id)
                    try:
                        self._clients['client_master'].update_page(client_id, {'サービス・商品': inverse})
                    except Exception as exc:
                        raise ProductLinkIssue('NOTION_WRITE_FAILED', 'client_master', client_id,
                                               permanent=False) from exc
                linked = relation_ids(self._page('product', product_id)['取引先マスター'])
                if not set(existing + [client_id]).issubset(linked):
                    raise ProductLinkIssue('INVERSE_NOT_VISIBLE', 'product', product_id, permanent=False)
                self._queue("finish_pair", project_id, product_id, client_id)

    def _deliver(self, project_id, db_key, notion_id):
        with acquire_record_sync_lock(self._store, db_key, notion_id):
            mapping = self._mapping(db_key, notion_id)
            values = self._page(db_key, notion_id)
            prop = next(iter(RELATIONS[db_key]))
            expected = self._queue("expected_ids", project_id, db_key, notion_id)
            if not set(expected).issubset(relation_ids(values[prop])):
                attempts = self._queue("unconfirmed", project_id, db_key, notion_id)
                raise ProductLinkIssue('DELIVERY_NOT_VISIBLE', db_key, notion_id, permanent=attempts >= 8)
            try:
                result = self._propagate(mapping, prop, relation_ids(values[prop]), values)
            except Exception as exc:
                raise ProductLinkIssue('DELIVERY_FAILED', db_key, notion_id, permanent=False) from exc
            self._queue("finish_delivery", project_id, db_key, notion_id)
            return result

    def _failure(self, project_id, exc, *, evaluation=False):
        if isinstance(exc, ProductLinkPersistenceError):
            raise exc
        issue = exc if isinstance(exc, ProductLinkIssue) else ProductLinkIssue(
            'RECORD_BUSY' if isinstance(exc, RecordSyncBusy) else 'PROCESSING_FAILED',
            'project', project_id, permanent=False,
        )
        self._queue("fail", project_id, issue.code, db_key=issue.db_key, notion_id=issue.notion_id,
                        permanent=issue.permanent, evaluation=evaluation)
        logger.warning('案件商品関連: code=%s db=%s id=%s held=%s',
                       issue.code, issue.db_key, issue.notion_id, issue.permanent)
        return issue

    def prepare(self, project: IdMapping) -> None:
        """Notion成立直後、他の配送や通知より先に再処理を確定する。"""
        self._queue("enqueue", project.notion_key)

    def __call__(self, project: IdMapping) -> tuple:
        # 起票・進捗保存に失敗した場合は例外を隠さない。
        project_id = project.notion_key
        self.prepare(project)
        deadline = self._clock() + self._time_budget
        errors, results = [], []
        task = self._queue("get", project_id)
        evaluation_ok = not task['evaluationHeld']
        if evaluation_ok:
            try:
                self._plan(project)
            except Exception as exc:
                errors.append(self._failure(project_id, exc, evaluation=True))
                evaluation_ok = False
        if evaluation_ok:
            for product_id, client_id in self._queue("pairs", project_id, self._pair_limit):
                if self._clock() >= deadline:
                    break
                try:
                    self._apply_pair(project_id, product_id, client_id)
                except Exception as exc:
                    issue = self._failure(project_id, exc)
                    if issue.permanent:
                        self._queue("hold_pair", project_id, product_id, client_id, issue.code, issue.db_key, issue.notion_id)
                    errors.append(issue)
        # 案件の現状態や追加処理の失敗に関係なく、発生済みの配送は消化する。
        for db_key, notion_id in self._queue("deliveries", project_id, self._delivery_limit):
            if self._clock() >= deadline:
                break
            try:
                results.append(self._deliver(project_id, db_key, notion_id))
            except Exception as exc:
                issue = self._failure(project_id, exc)
                if issue.permanent:
                    self._queue("hold_delivery", project_id, db_key, notion_id, issue.code)
                errors.append(issue)
        # 計画取得失敗時は旧ペア集合が空でも案件タスクを完了させない。
        if not errors:
            if not self._queue("complete", project_id):
                self._queue("defer", project_id)
        if errors:
            raise errors[0]
        return tuple(results)
