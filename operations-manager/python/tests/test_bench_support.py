"""Tests for the timing arithmetic behind the RC-117 measurements.

A benchmark whose summary is computed inline is a number nobody can check, so the
aggregation lives in ``scripts/bench_support`` and is pinned here. The cases that
matter are the ones where a plausible-looking implementation quietly reports
something that never happened: an interpolated percentile, a phase whose failure
was dropped from the samples, or a share column that does not add up.
"""

from __future__ import annotations

import pytest
from scripts.bench_support import PhaseTimer, format_table, median, percentile


def test_percentile_is_nearest_rank_not_interpolated() -> None:
    """p95 of five samples is the largest one, not a value between two of them.

    Interpolation would report a duration no run took, which is exactly what a
    measurement report must not do.
    """
    samples = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert percentile(samples, 0.95) == 5.0
    assert percentile(samples, 0.5) == 3.0
    assert percentile(samples, 0.2) == 1.0


def test_percentile_ignores_input_order() -> None:
    assert percentile([5.0, 1.0, 3.0], 0.5) == percentile([1.0, 3.0, 5.0], 0.5)


def test_percentile_rejects_empty_and_out_of_range() -> None:
    with pytest.raises(ValueError):
        percentile([], 0.5)
    with pytest.raises(ValueError):
        percentile([1.0], 0.0)
    with pytest.raises(ValueError):
        percentile([1.0], 1.5)


def test_median_averages_the_two_middle_values_when_even() -> None:
    assert median([1.0, 2.0, 3.0, 4.0]) == 2.5
    assert median([1.0, 2.0, 3.0]) == 2.0


def test_stats_summarise_every_recorded_sample() -> None:
    timer = PhaseTimer()
    for value in (0.1, 0.2, 0.3, 0.4):
        timer.record("push", value)

    stats = timer.stats("push")
    assert stats.count == 4
    assert stats.total == pytest.approx(1.0)
    assert stats.mean == pytest.approx(0.25)
    assert stats.median == pytest.approx(0.25)
    assert stats.maximum == pytest.approx(0.4)


def test_measure_records_even_when_the_block_raises() -> None:
    """A phase that failed still consumed time.

    Dropping it would understate exactly the case worth seeing: the slow attempt
    that ended in an error.
    """
    timer = PhaseTimer()
    with pytest.raises(RuntimeError):
        with timer.measure("push"):
            raise RuntimeError("push rejected")

    assert timer.stats("push").count == 1


def test_unrecorded_phase_raises_instead_of_reporting_zero() -> None:
    """Asking for a phase that never ran is a bug in the benchmark, not a 0.0s result."""
    timer = PhaseTimer()
    with pytest.raises(KeyError):
        timer.stats("push")


def test_all_stats_orders_by_total_cost() -> None:
    """'Where does the time go' is answered by the total per phase, not the mean.

    One expensive call and a hundred cheap ones: the cheap phase has the smaller
    mean and the larger bill, and it has to come first.
    """
    timer = PhaseTimer()
    timer.record("one slow call", 1.0)
    for _ in range(100):
        timer.record("many cheap calls", 0.05)

    ordered = [s.name for s in timer.all_stats()]
    assert ordered == ["many cheap calls", "one slow call"]


def test_format_table_shares_add_up_to_a_hundred() -> None:
    timer = PhaseTimer()
    timer.record("push", 1.0)
    timer.record("commit", 3.0)

    table = format_table(timer.all_stats())
    lines = table.splitlines()
    assert "commit" in lines[2] and "75.0%" in lines[2]
    assert "push" in lines[3] and "25.0%" in lines[3]


def test_format_table_handles_no_samples() -> None:
    assert format_table([]) == "(no samples)"
