"""The local E2E app must never probe a cluster with a blocking subprocess.

Why this guard exists. ``KubectlConnector.__init__`` runs
``subprocess.run(["kubectl", "auth", "whoami"], timeout=10)`` synchronously, on whatever
thread constructs it -- in this harness the uvicorn event loop that serves every request.
There is no cluster here, but on any machine that HAS a kubectl binary the probe does not
fail fast: it hangs the full 10 seconds and every request in flight waits behind it.

That produced failures that read exactly like a test leaking state, and are not. The
connector is a process-wide singleton, so the stall happens once per run, on whichever
test first touches a page that builds one -- a different test in every shuffle. The
neighbouring test's own 10-second wait then expires too. Measured on this suite (seed
404): ``test_saves_description_change`` died on a step POST at exactly 10s next to
``Error testing kubectl connection ... timed out after 10 seconds``, and took
``test_detail_page_renders`` with it because it never reached the restore in its
``finally``. Seeds 101/202/303, where the probe never happened to run, were green.

So the guard is not "kubectl is mocked" as a matter of tidiness: it is the difference
between a suite that is order-independent and one that only looks flaky.
"""

from __future__ import annotations

import pytest
from opi.connectors.kubectl import KubectlConnector

pytestmark = pytest.mark.e2e


def test_the_kubectl_singleton_is_already_initialized(app_server: str) -> None:
    """Starting the test app leaves a connector that will not probe on first use."""
    assert KubectlConnector._instance is not None, (
        "the test server did not pre-build the kubectl singleton; the first route that "
        "constructs one will run the blocking 10s probe on the event loop"
    )
    assert KubectlConnector._instance._initialized is True, (
        "the singleton is not marked initialized, so __init__ will still probe"
    )


def test_constructing_the_connector_runs_no_subprocess(app_server: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The construction a route does must not shell out.

    Teeth: drop the autouse fixture in ``tests/e2e/conftest.py`` and this fails, because
    ``__init__`` then reaches ``subprocess.run`` -- the call that blocks the loop for 10
    seconds.

    The calls are RECORDED, not raised on: ``KubectlConnector.__init__`` wraps the probe
    in ``except Exception``, so an exception raised from the stub would be swallowed and
    the test would pass while the probe still happened.
    """
    import subprocess

    calls: list[tuple] = []

    def _record(*args: object, **kwargs: object) -> None:
        calls.append(args)
        raise OSError("stubbed: the E2E app must not shell out")

    monkeypatch.setattr(subprocess, "run", _record)

    KubectlConnector()

    assert calls == [], f"the app shelled out while constructing KubectlConnector: {calls}"
