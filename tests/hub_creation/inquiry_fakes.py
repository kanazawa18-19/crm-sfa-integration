"""入口の保存判定だけを検証するための同期引継ぎフェイク。"""
from contextlib import contextmanager


class MemoryHandoff:
    def __init__(self):
        self.staged = []
        self.delivered = []

    @contextmanager
    def session(self, payload):
        yield self

    @contextmanager
    def page_lock(self, page_id):
        yield

    def ambiguous(self, reason):
        return {'state': 'blocked', 'reason': reason}

    def check_page(self, contact, page_id):
        return None

    def saved_page(self):
        return None

    def stage(self, page, properties):
        self.staged.append((page, dict(properties)))

    def create(self, client, properties):
        return client.create_page(properties)

    def deliver(self, client, page):
        self.delivered.append(page)
        return {'state': 'blocked', 'reason': '試験用保留'}
