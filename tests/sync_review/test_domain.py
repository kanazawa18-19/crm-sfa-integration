import pytest
from src.sync_review.domain import decide, is_blank, snapshot_hash, ReviewConflict, ReviewForbidden


def transition(state, action, **changes):
    return decide(state=state, action=action, is_manager=True,
                  expected_revision=changes.get("expected", 1), actual_revision=1)


def test_delete_requires_two_separate_steps():
    with pytest.raises(ReviewConflict):
        transition("pending", "approve")
    assert transition("pending", "confirm").state == "confirmed"
    assert transition("confirmed", "approve").state == "approved"
    with pytest.raises(ReviewConflict):
        transition("confirmed", "approve", expected=0)


@pytest.mark.parametrize("state", ["pending", "confirmed", "approved", "done", "failed"])
def test_manager_required_even_for_retry(state):
    with pytest.raises(ReviewForbidden):
        decide(state=state, action="resume", is_manager=False, expected_revision=1, actual_revision=1)


@pytest.mark.parametrize("state", ["pending", "confirmed"])
def test_rejection_retains_per_record_choice(state):
    assert transition(state, "keep_blank").state == "kept_blank"
    assert transition(state, "restore").state == "restore_requested"


@pytest.mark.parametrize("value", [None, "", [], {}])
def test_blank(value):
    assert is_blank(value)


@pytest.mark.parametrize("value", [0, False, "0", ["id"], " "])
def test_values_are_not_blank(value):
    assert not is_blank(value)


def test_snapshot_hash_preserves_types_and_ignores_dictionary_order():
    assert snapshot_hash({"a": 1, "b": 2}) == snapshot_hash({"b": 2, "a": 1})
    assert snapshot_hash({"a": 0}) != snapshot_hash({"a": False})
