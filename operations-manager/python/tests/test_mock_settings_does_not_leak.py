"""mock_settings must not leak into a module that a test imports while it is active.

Runs in a fresh interpreter: inside the suite an earlier test has usually imported the
module already, which is exactly how the leak stayed hidden in the full run and only
showed in a selection (test_sanitize.py after test_geen_uitzondering_via_de_andere_deuren.py).
"""

import subprocess
import sys
from pathlib import Path

PYTHON_ROOT = Path(__file__).resolve().parent.parent

SCRIPT = """
from unittest.mock import MagicMock, patch

import tests.conftest  # noqa: F401

with patch("opi.core.config.settings"):
    from opi.api import resource_router

assert not isinstance(resource_router.settings, MagicMock), "resource_router bound the mocked settings"
"""


def test_a_module_imported_under_mock_settings_keeps_the_real_settings() -> None:
    result = subprocess.run(
        [sys.executable, "-c", SCRIPT], cwd=PYTHON_ROOT, capture_output=True, text=True, timeout=120, check=False
    )

    assert result.returncode == 0, result.stderr[-2000:]
