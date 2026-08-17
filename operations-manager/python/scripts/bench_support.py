"""Timing collection and reporting for the project-write benchmarks.

Kept separate from the benchmark drivers so the arithmetic that turns raw samples
into the numbers quoted in a report is itself testable. A benchmark whose summary
is computed inline is a number nobody can check.

Two things live here:

* ``PhaseTimer`` -- records named samples and answers the only questions a
  measurement report should ask of them: how many, how long in total, and what a
  typical and a bad case look like.
* ``format_table`` -- renders a set of phases sorted by total cost, because "where
  does the time go" is answered by the total per phase, not by the mean.

Percentiles use nearest-rank on the sorted samples: with the handful of samples a
ten-action run produces, interpolating between two of them invents a value that was
never measured.
"""

from __future__ import annotations

import time
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field


@dataclass(frozen=True)
class PhaseStats:
    """Aggregate of every sample recorded under one phase name."""

    name: str
    count: int
    total: float
    mean: float
    median: float
    p95: float
    maximum: float


def percentile(samples: list[float], fraction: float) -> float:
    """Nearest-rank percentile of ``samples``. ``fraction`` is 0..1.

    Nearest-rank on purpose: an interpolated percentile over five samples reports a
    duration that no run actually took.
    """
    if not samples:
        raise ValueError("percentile of an empty sample set")
    if not 0.0 < fraction <= 1.0:
        raise ValueError(f"fraction must be in (0, 1], got {fraction}")
    ordered = sorted(samples)
    rank = max(1, -(-len(ordered) * fraction // 1))  # ceil without importing math
    return ordered[int(rank) - 1]


def median(samples: list[float]) -> float:
    """Median of ``samples``; the mean of the two middle values when even."""
    if not samples:
        raise ValueError("median of an empty sample set")
    ordered = sorted(samples)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


@dataclass
class PhaseTimer:
    """Collects wall-clock samples per named phase."""

    samples: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))

    def record(self, phase: str, seconds: float) -> None:
        self.samples[phase].append(seconds)

    @contextmanager
    def measure(self, phase: str):
        """Time the enclosed block and record it under ``phase``.

        Records on the way out even when the block raises: a phase that failed
        still consumed time, and dropping it would understate exactly the case
        worth seeing.
        """
        started = time.monotonic()
        try:
            yield
        finally:
            self.record(phase, time.monotonic() - started)

    def stats(self, phase: str) -> PhaseStats:
        values = self.samples[phase]
        if not values:
            raise KeyError(f"no samples recorded for phase '{phase}'")
        return PhaseStats(
            name=phase,
            count=len(values),
            total=sum(values),
            mean=sum(values) / len(values),
            median=median(values),
            p95=percentile(values, 0.95),
            maximum=max(values),
        )

    def all_stats(self) -> list[PhaseStats]:
        """Every phase, most expensive in total first."""
        return sorted((self.stats(name) for name in self.samples), key=lambda s: s.total, reverse=True)


def format_table(stats: list[PhaseStats], *, total_label: str = "phase") -> str:
    """Render phase statistics as a fixed-width table, with a share-of-total column.

    The share is of the summed phase totals, not of wall clock: phases can nest
    (a push happens inside a lock hold), so wall clock would make the shares add
    up to more than the run. Callers that mix nested and flat phases should render
    them in separate tables.
    """
    if not stats:
        return "(no samples)"

    grand_total = sum(s.total for s in stats)
    width = max(len(total_label), max(len(s.name) for s in stats))
    lines = [f"{total_label:<{width}}  {'n':>3}  {'total':>8}  {'share':>6}  {'mean':>8}  {'p50':>8}  {'p95':>8}"]
    lines.append("-" * len(lines[0]))
    for s in stats:
        share = (s.total / grand_total * 100) if grand_total else 0.0
        lines.append(
            f"{s.name:<{width}}  {s.count:>3}  {s.total:>7.3f}s  {share:>5.1f}%  "
            f"{s.mean:>7.3f}s  {s.median:>7.3f}s  {s.p95:>7.3f}s"
        )
    return "\n".join(lines)
