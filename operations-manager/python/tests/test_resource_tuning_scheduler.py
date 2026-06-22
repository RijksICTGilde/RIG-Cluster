"""Tests for the resource tuning scheduler's due-project rotation."""

from opi.core.resource_tuning_scheduler import _rotate_batch


class TestRotateBatch:
    def test_empty(self):
        assert _rotate_batch([], 0, 5) == ([], 0)

    def test_smaller_than_cap_returns_all(self):
        batch, cursor = _rotate_batch(["a", "b"], 0, 5)
        assert batch == ["a", "b"]
        assert cursor == 0  # wrapped back to start

    def test_advances_cursor(self):
        items = ["a", "b", "c", "d", "e", "f", "g"]
        batch, cursor = _rotate_batch(items, 0, 3)
        assert batch == ["a", "b", "c"]
        assert cursor == 3

    def test_full_coverage_over_ticks_without_cursor_reset(self):
        # The starvation regression: with a fixed list and no external state
        # change, rotation must still reach every item across ticks.
        items = [f"p{i}" for i in range(18)]
        cap = 5
        cursor = 0
        seen: set[str] = set()
        for _ in range(4):  # ceil(18/5) = 4 ticks
            batch, cursor = _rotate_batch(items, cursor, cap)
            seen.update(batch)
        assert seen == set(items)

    def test_wraps_around(self):
        items = ["a", "b", "c", "d", "e"]
        batch, cursor = _rotate_batch(items, 3, 3)
        assert batch == ["d", "e", "a"]
        assert cursor == 1
