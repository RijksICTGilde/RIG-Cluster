"""Makes visible when the asyncio event loop stalls, and what holds it.

FastAPI, the TaskWorker and every scheduler share one loop, so a synchronous block hits web
requests, every running task and the schedulers at once. The health probes do not see it:
those run on a thread of their own (``opi/core/probe_server.py``).

A heartbeat task on the loop writes a timestamp every ``_HEARTBEAT`` seconds. Whatever sits
above ``_HEARTBEAT`` is the time the loop did not get round to the task, and the task knows it
exactly as soon as it gets its turn again. Those seconds go onto a counter that only goes up;
``current_lag`` adds what a stall that is still running costs so far.

The warning comes from a daemon thread that reads the same timestamp. That MUST be a thread:
during the stall there is no task that can report anything, and that is exactly when the stack
of the main thread is the answer to which line holds the loop.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
import traceback

from opi.core.config import settings

logger = logging.getLogger(__name__)

#: How often the heartbeat task sets the clock, and how often the thread looks. Writing a
#: float a hundred times a second costs nothing and makes a stall of 200 ms visible already.
_HEARTBEAT = 0.1

_heartbeat_task: asyncio.Task[None] | None = None
_thread: threading.Thread | None = None
_stopping = threading.Event()

#: Last timestamp of the heartbeat task, and the main thread whose stack matters. Both are
#: read by a thread; reading a float and an int is atomic enough, and a lock here could only
#: make the measurement itself wait.
_last_heartbeat: float = 0.0
_main_thread_id: int = 0

#: Cumulative lag and the stall counter. See metrics.py on why /metrics needs a counter
#: without reset-on-read.
_total_lag: float = 0.0
_stalls: int = 0
_lock = threading.Lock()


def _add_lag(lag: float) -> None:
    global _total_lag
    if lag <= 0:
        return
    with _lock:
        _total_lag += lag


def total_lag() -> float:
    """All lag the loop has run up since it started, in seconds."""
    with _lock:
        return _total_lag


def stall_count() -> int:
    """How often the lag came out above ``EVENT_LOOP_LAG_WARNING_SECONDS``."""
    with _lock:
        return _stalls


def current_lag() -> float:
    """The lag right now: how long the heartbeat has gone without its turn.

    A stall that is still running during the scrape is not in ``total_lag`` yet, because the
    heartbeat only adds it once it runs again. Here it is already visible.
    """
    if not _last_heartbeat:
        return 0.0
    return max(0.0, time.monotonic() - _last_heartbeat - _HEARTBEAT)


async def _heartbeat() -> None:
    global _last_heartbeat
    while True:
        now = time.monotonic()
        # The task itself measures the gap exactly: the watcher only sees what had passed at
        # its sampling moment, and so misses up to one heartbeat of the tail.
        _add_lag(now - _last_heartbeat - _HEARTBEAT)
        _last_heartbeat = now
        await asyncio.sleep(_HEARTBEAT)


def _watch() -> None:
    global _stalls
    reported = False
    while not _stopping.wait(_HEARTBEAT):
        lag = current_lag()
        if lag < settings.EVENT_LOOP_LAG_WARNING_SECONDS:
            reported = False
            continue
        if reported:
            # Once per stall, not again every 100 ms: the stack does not change as long as
            # the same line holds the loop.
            continue
        reported = True
        with _lock:
            _stalls += 1
        logger.warning("De event loop staat %.1fs stil. Stack van de hoofdthread:\n%s", lag, _main_thread_stack())


def _main_thread_stack() -> str:
    frame = sys._current_frames().get(_main_thread_id)
    if frame is None:
        return "  (hoofdthread niet gevonden)"
    return "".join(traceback.format_stack(frame))


def start_loop_watchdog() -> None:
    """Start the heartbeat and the watcher. Safe to call more than once."""
    global _heartbeat_task, _thread, _main_thread_id, _last_heartbeat

    if _heartbeat_task is None or _heartbeat_task.done():
        _last_heartbeat = time.monotonic()
        _heartbeat_task = asyncio.create_task(_heartbeat())

    if _thread is None or not _thread.is_alive():
        _main_thread_id = threading.main_thread().ident or 0
        _stopping.clear()
        _thread = threading.Thread(target=_watch, name="loop-watchdog", daemon=True)
        _thread.start()
        logger.info(
            "Event-loop watchdog gestart (hartslag %.1fs, waarschuwing vanaf %.1fs)",
            _HEARTBEAT,
            settings.EVENT_LOOP_LAG_WARNING_SECONDS,
        )


def stop_loop_watchdog() -> None:
    """Stop the heartbeat and the watcher."""
    global _heartbeat_task, _thread, _last_heartbeat

    _stopping.set()
    if _heartbeat_task is not None and not _heartbeat_task.done():
        _heartbeat_task.cancel()
    _heartbeat_task = None
    # Without this the last timestamp stays put and current_lag keeps growing: a scrape
    # during the shutdown would report a stall that is not there.
    _last_heartbeat = 0.0
    if _thread is not None:
        _thread.join(timeout=1.0)
    _thread = None
