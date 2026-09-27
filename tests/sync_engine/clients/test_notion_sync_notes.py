"""メモは先頭へ置き、本文を保持し、再送で重複追加しない。"""
from src.sync_engine.clients.notion_client import HttpNotionClient
from src.sync_engine.sync_notes import CHOICES, render_notes

BASE = "https://api.notion.com/v1"
NOTES = {f"{CHOICES}|zoho:担当": "[zoho:担当] 対応先の確認が必要"}


def client():
    return HttpNotionClient("chain", "db", api_key="test-key")


def block(block_id="memo"):
    return {"id": block_id, "type": "callout", "callout": {
        "rich_text": [{"text": {"content": render_notes(NOTES)}}]}}


def test_insert_is_at_start_with_existing_body(requests_mock):
    requests_mock.get(BASE + "/blocks/page/children?page_size=100", json={
        "results": [{"id": "body", "type": "paragraph"}], "has_more": False})
    post = requests_mock.patch(BASE + "/blocks/page/children", json={"results": [block()]})
    client().upsert_sync_notes("page", NOTES)
    assert post.last_request.json()["position"] == {"type": "start"}
    assert len(post.last_request.json()["children"]) == 1
    assert requests_mock.call_count == 2


def test_same_notes_do_not_write_again(requests_mock):
    requests_mock.get(BASE + "/blocks/page/children?page_size=100", json={
        "results": [block(), {"id": "body", "type": "paragraph"}], "has_more": False})
    client().upsert_sync_notes("page", NOTES)
    assert requests_mock.call_count == 1


def test_moved_memo_is_replaced_at_top_without_changing_body(requests_mock):
    requests_mock.get(BASE + "/blocks/page/children?page_size=100", json={
        "results": [{"id": "body", "type": "paragraph"}, block("old")], "has_more": False})
    requests_mock.patch(BASE + "/blocks/page/children", json={"results": [block("new")]})
    archive = requests_mock.patch(BASE + "/blocks/old", json={})
    client().upsert_sync_notes("page", NOTES)
    assert archive.last_request.json() == {"archived": True}
    assert requests_mock.call_count == 3


def test_empty_page_gets_same_top_insert(requests_mock):
    requests_mock.get(BASE + "/blocks/page/children?page_size=100", json={"results": [], "has_more": False})
    append = requests_mock.patch(BASE + "/blocks/page/children", json={"results": [block()]})
    client().upsert_sync_notes("page", NOTES)
    assert append.last_request.json()["position"] == {"type": "start"}
