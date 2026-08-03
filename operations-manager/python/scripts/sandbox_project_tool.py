#!/usr/bin/env python3
"""Fast, HTTP-only helpers for driving sandbox projects without launching a browser.

Scraping a project's API key via Playwright means launching Chromium every time, which
is slow. This tool signs the same Starlette session cookie the E2E suite uses and talks
plain HTTP, so it returns in seconds. Use it to grab an API key, delete a project through
the Operations Manager, or set a service config.

All actions go through the Operations Manager API/UI -- no raw kubectl, so deletes run the
proper OPI teardown.

Env (defaults match the sandbox):
    ZAD_BASE_URL   default https://zad.sandbox.rijksapp.dev
    ZAD_SECRET_KEY default sandbox-e2e-test-secret-key-min32chars  (must equal the pod SECRET_KEY)
    ZAD_USER_EMAIL default admin@sandbox.rijksapp.dev

Examples:
    uv run python scripts/sandbox_project_tool.py api-key myproject
    uv run python scripts/sandbox_project_tool.py delete  myproject
    uv run python scripts/sandbox_project_tool.py set-config myproject postgresql-database project \
        '{"scope":"project","storage":"256Mi","instances":1}'
"""

from __future__ import annotations

import base64
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request

from itsdangerous import TimestampSigner

BASE = os.environ.get("ZAD_BASE_URL", "https://zad.sandbox.rijksapp.dev").rstrip("/")
SECRET = os.environ.get("ZAD_SECRET_KEY", "sandbox-e2e-test-secret-key-min32chars")
USER_EMAIL = os.environ.get("ZAD_USER_EMAIL", "admin@sandbox.rijksapp.dev")

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE


def _session_cookie() -> str:
    user = {"sub": "sandbox-tool", "email": USER_EMAIL, "name": "Sandbox Tool"}
    payload = base64.b64encode(json.dumps({"user": user}).encode()).decode()
    return "session=" + TimestampSigner(SECRET).sign(payload).decode()


def _request(
    path: str, *, method: str = "GET", api_key: str | None = None, body: dict | None = None
) -> tuple[int, str]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    else:
        headers["Cookie"] = _session_cookie()
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{BASE}{path}", data=data, method=method, headers=headers)  # noqa: S310 (fixed https base)
    try:
        with urllib.request.urlopen(req, context=_CTX, timeout=120) as resp:  # noqa: S310
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def get_api_key(project: str) -> str:
    """Scrape a project's decrypted API key from its details page (session-cookie auth).

    The key is a 32-char token rendered in a ROOS secret-field; the AGE key on the same
    page is longer, so a strict 32-char match picks the API key unambiguously.
    """
    status, html = _request(f"/projects/details/{project}")
    if status != 200:
        raise SystemExit(f"details page for '{project}' returned {status}")
    for value in (v.strip() for v in re.findall(r"roos-secret-field__value[^>]*>(.*?)</", html, re.DOTALL)):
        if re.fullmatch(r"[A-Za-z0-9_\-]{32}", value):
            return value
    raise SystemExit(f"no API key found on the details page for '{project}'")


def delete_project(project: str) -> None:
    key = get_api_key(project)
    status, text = _request(
        f"/api/projects/{project}", method="DELETE", api_key=key, body={"confirmDeletion": True, "force": True}
    )
    print(f"DELETE {project} -> {status} {text[:200]}")


def set_config(project: str, service: str, target: str, config_json: str) -> None:
    key = get_api_key(project)
    body = json.loads(config_json)
    status, text = _request(
        f"/api/v2/projects/{project}/services/{service}/config/{target}", method="PUT", api_key=key, body=body
    )
    print(f"PUT {service}/{target} on {project} -> {status} {text[:200]}")


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    cmd = argv[1]
    if cmd == "api-key" and len(argv) == 3:
        print(get_api_key(argv[2]))
    elif cmd == "delete" and len(argv) == 3:
        delete_project(argv[2])
    elif cmd == "set-config" and len(argv) == 6:
        set_config(argv[2], argv[3], argv[4], argv[5])
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
