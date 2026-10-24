"""De proefprojecten op het sandboxdoel: aanmaken voor een run, opruimen erna.

Zonder projecten met een sleutel meet de proef niet of een project bij een ander kan. Ze
komen door de echte wizard (de helpers van de e2e), zodat ze er net zo uitzien als een
project van een gebruiker; de sleutels gaan naar ``.proef/`` en nooit naar de uitvoer.

    uv run python -m security_tests.proefprojecten maak [AANTAL]
    uv run python -m security_tests.proefprojecten ruim-op
"""

from __future__ import annotations

import json
import os
import sys
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright
from tests.e2e.conftest import (
    E2E_SECRET_KEY,
    FORGEJO_PASSWORD,
    FORGEJO_PROJECTS_REPO,
    FORGEJO_URL,
    FORGEJO_USER,
    FORGEJO_VERIFY_SSL,
    SANDBOX_TEST_USER,
    _sign_session,
)
from tests.e2e.helpers import sandbox_api
from tests.e2e.helpers.forgejo import ForgejoClient
from tests.e2e.helpers.lifecycle import ALL_CREATE_WIZARD_SERVICES, create_project_with_services
from tests.e2e.helpers.wizard import unique_project_name

from security_tests.doel import PROEF_ROOT

#: Waar de sleutels van deze run staan; ``security-proof:sandbox`` geeft ze door als ZAD_PROEF_SLEUTELS.
SLEUTELBESTAND = PROEF_ROOT / ".proef" / "proefprojecten.json"


def _basis() -> str:
    return os.environ.get("E2E_BASE_URL", "https://zad.sandbox.rijksapp.dev").rstrip("/")


def maak(aantal: int) -> int:
    """Maak ``aantal`` proefprojecten en schrijf hun sleutels weg."""
    basis = _basis()
    forgejo = ForgejoClient(
        base_url=FORGEJO_URL,
        username=FORGEJO_USER,
        password=FORGEJO_PASSWORD,
        repo=FORGEJO_PROJECTS_REPO,
        verify_ssl=FORGEJO_VERIFY_SSL,
    )
    sleutels: dict[str, str] = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        context = browser.new_context(ignore_https_errors=True)
        context.add_cookies(
            [
                {
                    "name": "session",
                    "value": _sign_session({"user": SANDBOX_TEST_USER}, secret=E2E_SECRET_KEY),
                    "domain": urlparse(basis).hostname or "localhost",
                    "path": "/",
                }
            ]
        )
        page = context.new_page()
        try:
            for i in range(aantal):
                # Alle diensten van de wizard, zoals test_sandbox_all_services: dan heeft elk
                # proefproject een webadres, een database, een bucket en de rest, en meet de
                # proef de isolatie tussen twee projecten op elke dienst.
                project = create_project_with_services(
                    page,
                    basis,
                    forgejo,
                    unique_project_name(prefix=f"zap{i}"),
                    user_email=SANDBOX_TEST_USER["email"],
                    services=ALL_CREATE_WIZARD_SERVICES,
                )
                sleutels[project.name] = project.api_key
                print(f"proefproject {i + 1}/{aantal}: {project.name}")
        finally:
            # Ook bij een fout halverwege: wat er al staat moet ruim-op kunnen vinden.
            SLEUTELBESTAND.parent.mkdir(parents=True, exist_ok=True)
            SLEUTELBESTAND.write_text(json.dumps(sleutels))
            SLEUTELBESTAND.chmod(0o600)
            browser.close()
    return 0 if len(sleutels) == aantal else 1


def ruim_op() -> int:
    """Verwijder de proefprojecten van deze run via hun eigen sleutel."""
    if not SLEUTELBESTAND.exists():
        print("geen proefprojecten om op te ruimen")
        return 0
    sleutels: dict[str, str] = json.loads(SLEUTELBESTAND.read_text() or "{}")
    fout = 0
    for naam, sleutel in sleutels.items():
        try:
            sandbox_api.delete_project_via_api(_basis(), naam, sleutel, verify_ssl=False)
            print(f"opgeruimd: {naam}")
        except (AssertionError, OSError, RuntimeError) as reden:
            fout = 1
            print(f"NIET opgeruimd: {naam}: {reden}")
    if not fout:
        SLEUTELBESTAND.unlink()
    return fout


if __name__ == "__main__":
    opdracht = sys.argv[1] if len(sys.argv) > 1 else ""
    if opdracht == "maak":
        raise SystemExit(maak(int(sys.argv[2]) if len(sys.argv) > 2 else 2))
    if opdracht == "ruim-op":
        raise SystemExit(ruim_op())
    raise SystemExit("gebruik: python -m security_tests.proefprojecten maak [AANTAL] | ruim-op")
