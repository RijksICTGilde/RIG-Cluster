"""Maak van elke pagina een screenshot en een DOM-afdruk, om een verbouwing te bewijzen.

Bij het opdelen van templates verandert er per definitie niets aan wat er op het scherm
staat: elke stap is een verplaatsing. Dat is precies het soort verandering waar een
unittest niets over zegt en een plaatje alles. Dit script draait de bestaande E2E-testapp
(echte FastAPI, gemockte buitenwereld) en legt van elke pagina twee dingen vast:

- ``<naam>.png``  - hoe de pagina eruitziet;
- ``<naam>.html`` - de DOM na het laden, genormaliseerd zodat wisselende waarden
  (versienummers, ids, tijden) geen ruis geven.

Gebruik: maak een afdruk op de basiscommit, maak er een op je werk, en vergelijk.

    git worktree add /tmp/basis <basis-commit>
    (cd /tmp/basis/operations-manager/python && uv run python scripts/template_snapshot.py /tmp/snap-basis)
    uv run python scripts/template_snapshot.py /tmp/snap-nu
    diff -ru /tmp/snap-basis /tmp/snap-nu

De HTML-afdruk is de scherpe vergelijking (diff wijst de regel aan); de screenshot vangt
wat de DOM niet laat zien, zoals vormgeving die van plaats verschoven is.
"""

import argparse
import base64
import contextlib
import json
import re
import socket
import sys
import threading
import time
from pathlib import Path

import uvicorn
from itsdangerous import TimestampSigner
from playwright.sync_api import sync_playwright
from tests.e2e.testserver import SECRET_KEY, create_test_app

# Elke pagina die een template met een eigen content-blok rendert. De naam is de
# bestandsnaam van de afdruk.
PAGES: list[tuple[str, str]] = [
    ("home", "/"),
    ("dashboard", "/dashboard"),
    ("projects-overview", "/projects"),
    ("project-details", "/projects/test-project-detail/details"),
    ("services-overview", "/services"),
    ("architecture", "/architecture"),
    ("about", "/about"),
    ("tools", "/tools"),
    ("metrics-explorer", "/metrics-explorer"),
    ("permission-denied", "/permission-denied"),
    ("admin-users", "/admin/users"),
    ("admin-approvals", "/admin/approvals"),
    ("admin-usage", "/admin/usage"),
    ("wizard-start", "/forms/wizard/create-project"),
]

# Waarden die per run verschillen en dus niets zeggen over een verbouwing.
NOISE: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r'\?v=[0-9a-f]+"'), '?v=HASH"'),
    (re.compile(r"(csrf_token\"? ?[=:] ?.?\"?)[A-Za-z0-9_-]{20,}"), r"\1CSRF"),
    (re.compile(r"(X-CSRF-Token[^A-Za-z0-9]{2,10})[A-Za-z0-9_-]{20,}"), r"\1CSRF"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"), "UUID"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?"), "TIJDSTIP"),
    (re.compile(r"\b\d{2}-\d{2}-\d{4}\b"), "DATUM"),
    (re.compile(r'(id|for|aria-labelledby|aria-controls)="[^"]*\d{3,}[^"]*"'), r'\1="GEGENEREERD"'),
]

TEST_USER = {"sub": "snapshot-user", "email": "test@example.com", "name": "Snapshot User"}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _session_cookie() -> str:
    payload = base64.b64encode(json.dumps({"user": TEST_USER}).encode()).decode()
    return TimestampSigner(SECRET_KEY).sign(payload).decode()


def _normalise(html: str) -> str:
    for pattern, replacement in NOISE:
        html = pattern.sub(replacement, html)
    # Een regel per tag maakt de diff leesbaar: een verplaatst blok wordt dan een blok
    # verplaatste regels in plaats van een enkele onleesbare megaregel.
    return re.sub(r">\s*<", ">\n<", html).strip() + "\n"


def _capture(base_url: str, out: Path) -> int:
    failures = 0
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(args=["--no-sandbox", "--disable-setuid-sandbox"])
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        host = base_url.split("//", 1)[1].split(":")[0]
        context.add_cookies([{"name": "session", "value": _session_cookie(), "domain": host, "path": "/"}])
        page = context.new_page()
        for name, path in PAGES:
            response = page.goto(f"{base_url}{path}", wait_until="networkidle")
            status = response.status if response else 0
            if status >= 400:
                print(f"  {name}: HTTP {status}", file=sys.stderr)
                failures += 1
            (out / f"{name}.html").write_text(_normalise(page.content()))
            page.screenshot(path=str(out / f"{name}.png"), full_page=True)
            print(f"  {name} ({path}) -> HTTP {status}")
        context.close()
        browser.close()
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("outdir", type=Path, help="map waar de afdrukken in komen")
    arguments = parser.parse_args()
    arguments.outdir.mkdir(parents=True, exist_ok=True)

    port = _free_port()
    with create_test_app()() as app:
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{port}"
        deadline = time.time() + 30
        while not server.started and time.time() < deadline:
            time.sleep(0.1)
        if not server.started:
            print("testapp startte niet", file=sys.stderr)
            return 1
        try:
            failures = _capture(base_url, arguments.outdir)
        finally:
            server.should_exit = True
            thread.join(timeout=10)

    print(f"afdrukken in {arguments.outdir}")
    return 1 if failures else 0


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        sys.exit(main())
    sys.exit(130)
