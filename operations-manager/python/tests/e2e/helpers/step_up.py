"""Een sessie of browsercontext met een VERSE step-up-stempel (RC-387/388).

Handelingen als een token uitgeven of het omgevingsvariabelen-venster openen vragen een
verse step-up, en de gedeelde sandbox-context heeft die stempel niet: daarmee strandt
elke zo'n handeling in de OTP-dialoog. De sandbox-admin registreert geen OTP, dus een
echte code is er in de test niet; wie de SECRET_KEY van het cluster kent zet de stempel
er zelf bij. De dialoog zelf dekt ``tests/e2e/test_env_vars_venster.py`` lokaal.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from tests.e2e.conftest import E2E_SECRET_KEY, SANDBOX_ACTIE_BUDGET_MS, SANDBOX_TEST_USER
from tests.e2e.helpers import sandbox_api

if TYPE_CHECKING:
    from playwright.sync_api import Browser, BrowserContext


def fresh_step_up_session() -> str:
    """Een ondertekende sessiecookie die een verse step-up-stempel draagt.

    De inhoud woont server-side (RC-390): deze maakt de rij aan in de sandbox-database
    en tekent alleen het id. De stempel zit dus in de rij, niet in de cookie.
    """
    return sandbox_api.make_session_cookie(
        {"user": SANDBOX_TEST_USER, sandbox_api.STEP_UP_SESSION_KEY: datetime.now(UTC).isoformat()},
        secret=E2E_SECRET_KEY,
    )


def context_with_fresh_step_up(browser: Browser, sandbox_url: str) -> BrowserContext:
    """Een eigen browsercontext met een sessie die een VERSE step-up draagt.

    Zo kan een suite meten wat ná de dialoog komt. De stempel veroudert, dus maak de
    context vlak voor de handeling aan in plaats van hem een hele suite te laten dragen.
    """
    context = browser.new_context(ignore_https_errors=True)
    context.set_default_timeout(SANDBOX_ACTIE_BUDGET_MS)
    context.set_default_navigation_timeout(SANDBOX_ACTIE_BUDGET_MS)
    parsed = urlparse(sandbox_url)
    context.add_cookies(
        [{"name": "session", "value": fresh_step_up_session(), "domain": parsed.hostname or "localhost", "path": "/"}]
    )
    return context
