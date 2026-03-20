"""Tests for the flow_id context variable and its integration with logging."""

import asyncio
import logging
import re

import pytest
from opi.core.flow_id import flow_id, get_flow_id, set_flow_id


class TestFlowIdContextVar:
    def test_default_is_dash(self) -> None:
        ctx = flow_id.get()
        assert ctx == "-"

    def test_set_flow_id_returns_prefixed_id(self) -> None:
        result = set_flow_id("req")
        assert result.startswith("req-")
        assert len(result) == len("req-") + 8

    def test_set_flow_id_updates_context(self) -> None:
        fid = set_flow_id("task")
        assert get_flow_id() == fid

    def test_different_calls_produce_different_ids(self) -> None:
        id1 = set_flow_id("req")
        id2 = set_flow_id("req")
        assert id1 != id2

    def test_id_is_valid_hex(self) -> None:
        fid = set_flow_id("x")
        hex_part = fid.split("-", 1)[1]
        assert re.fullmatch(r"[0-9a-f]{8}", hex_part)

    def test_custom_prefix(self) -> None:
        fid = set_flow_id("task-abc12345")
        assert fid.startswith("task-abc12345-")


class TestFlowIdAsyncIsolation:
    @pytest.mark.asyncio
    async def test_async_tasks_get_isolated_contexts(self) -> None:
        """Each asyncio.Task inherits a copy of the parent context at creation time,
        so setting flow_id inside a task does not leak to sibling tasks."""
        results: dict[str, str] = {}

        async def worker(name: str, prefix: str) -> None:
            set_flow_id(prefix)
            await asyncio.sleep(0.01)  # yield to let siblings run
            results[name] = get_flow_id()

        task_a = asyncio.create_task(worker("a", "alpha"))
        task_b = asyncio.create_task(worker("b", "beta"))
        await asyncio.gather(task_a, task_b)

        assert results["a"].startswith("alpha-")
        assert results["b"].startswith("beta-")
        assert results["a"] != results["b"]

    @pytest.mark.asyncio
    async def test_child_task_does_not_mutate_parent_context(self) -> None:
        parent_fid = set_flow_id("parent")

        async def child() -> None:
            set_flow_id("child")

        await asyncio.create_task(child())
        assert get_flow_id() == parent_fid


class TestLogRecordFactory:
    """Tests that the record factory (installed at import time by logging_config)
    injects flow_id into records produced by normal logger usage."""

    @pytest.fixture(autouse=True)
    def _ensure_factory(self) -> None:
        # Importing logging_config registers our custom LogRecord factory
        import opi.utils.logging_config  # noqa: F401

    @pytest.fixture
    def capture_handler(self) -> logging.Handler:
        """A handler that stores the last LogRecord it receives."""

        class _CaptureHandler(logging.Handler):
            record: logging.LogRecord | None = None

            def emit(self, record: logging.LogRecord) -> None:
                self.record = record

        return _CaptureHandler()

    def _make_logger(self, handler: logging.Handler) -> logging.Logger:
        logger = logging.getLogger("test.flow_id")
        logger.handlers = [handler]
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        return logger

    def test_log_record_has_flow_id_attribute(self, capture_handler: logging.Handler) -> None:
        logger = self._make_logger(capture_handler)
        logger.info("hello")
        assert hasattr(capture_handler.record, "flow_id")  # type: ignore[union-attr]

    def test_log_record_reflects_current_flow(self, capture_handler: logging.Handler) -> None:
        fid = set_flow_id("test")
        logger = self._make_logger(capture_handler)
        logger.info("hello")
        assert capture_handler.record.flow_id == fid  # type: ignore[union-attr]

    def test_log_record_default_when_no_flow(self, capture_handler: logging.Handler) -> None:
        flow_id.set("-")
        logger = self._make_logger(capture_handler)
        logger.info("hello")
        assert capture_handler.record.flow_id == "-"  # type: ignore[union-attr]

    def test_format_string_renders_flow_id(self, capture_handler: logging.Handler) -> None:
        fid = set_flow_id("fmt")
        formatter = logging.Formatter("[%(flow_id)s] %(message)s")
        capture_handler.setFormatter(formatter)
        logger = self._make_logger(capture_handler)
        logger.info("hello")
        output = formatter.format(capture_handler.record)  # type: ignore[arg-type]
        assert f"[{fid}]" in output
