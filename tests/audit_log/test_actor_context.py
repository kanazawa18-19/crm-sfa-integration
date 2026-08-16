from __future__ import annotations

from src.audit_log.actor_context import Actor, get_actor, set_actor


def test_get_actor_defaults_to_unknown() -> None:
    assert get_actor() == Actor(source="unknown", label=None)


def test_set_actor_sets_source_and_label_within_block() -> None:
    with set_actor("kintone_webhook", "kintone"):
        assert get_actor() == Actor(source="kintone_webhook", label="kintone")
    assert get_actor() == Actor(source="unknown", label=None)


def test_set_actor_label_defaults_to_none() -> None:
    with set_actor("gmail_sync"):
        assert get_actor() == Actor(source="gmail_sync", label=None)


def test_set_actor_restores_outer_value_after_nested_block() -> None:
    with set_actor("migration"):
        with set_actor("gmail_sync"):
            assert get_actor().source == "gmail_sync"
        assert get_actor().source == "migration"
    assert get_actor().source == "unknown"


def test_set_actor_restores_value_even_when_block_raises() -> None:
    with set_actor("migration"):
        try:
            with set_actor("gmail_sync"):
                raise ValueError("boom")
        except ValueError:
            pass
        assert get_actor().source == "migration"
