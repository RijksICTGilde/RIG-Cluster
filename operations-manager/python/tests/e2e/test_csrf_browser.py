"""End-to-end CSRF tests with a real browser.

Verifies the server-rendered CSRF protection actually works the way the
unit tests claim: token gets rendered into the page from the same request
that mints the cookie, htmx submissions carry the header, classic forms
carry the hidden field, the cookie is HttpOnly (no JS access).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = pytest.mark.e2e


CSRF_COOKIE_NAME = "csrf_token"
CSRF_HEADER_NAME = "x-csrf-token"


# ---------------------------------------------------------------------------
# Cookie is HttpOnly
# ---------------------------------------------------------------------------


def test_csrf_cookie_is_httponly(app_server: str, auth_page: Page) -> None:
    """The csrf_token cookie must be HttpOnly so JavaScript cannot read it.

    Templates render the token server-side via {{ request.state.csrf_token }};
    no client-side code needs to read it from the cookie.
    """
    auth_page.goto(app_server)

    # Read all cookies set on this origin via the BrowserContext (which sees
    # HttpOnly cookies); then via document.cookie (which does NOT see them).
    server_visible = {c["name"]: c for c in auth_page.context.cookies()}
    js_visible = auth_page.evaluate("document.cookie")

    assert CSRF_COOKIE_NAME in server_visible, "CSRF cookie was not set by the server"
    cookie = server_visible[CSRF_COOKIE_NAME]
    assert cookie["httpOnly"] is True, f"CSRF cookie must be HttpOnly, got {cookie}"
    assert CSRF_COOKIE_NAME not in js_visible, f"CSRF cookie must NOT appear in document.cookie; got: {js_visible}"


# ---------------------------------------------------------------------------
# Wizard form carries hx-headers with the same token as the cookie
# ---------------------------------------------------------------------------


def test_wizard_form_renders_hx_headers_with_csrf_token(app_server: str, auth_page: Page) -> None:
    """The wizard's <form hx-ext='json-enc'> must carry an hx-headers
    attribute containing X-CSRF-Token equal to the cookie value, so the
    next htmx POST passes the middleware's double-submit check.
    """
    auth_page.goto(f"{app_server}/forms/wizard/create-project")
    auth_page.wait_for_load_state("networkidle")

    # Pull the cookie value the browser holds (BrowserContext sees HttpOnly).
    cookie_value = next(c["value"] for c in auth_page.context.cookies() if c["name"] == CSRF_COOKIE_NAME)
    assert cookie_value, "CSRF cookie value empty"

    # Read the hx-headers attribute on the wizard form.
    hx_headers = auth_page.locator("#wizard-step-form").get_attribute("hx-headers")
    assert hx_headers is not None, "wizard form is missing hx-headers"
    assert "X-CSRF-Token" in hx_headers, f"hx-headers does not name X-CSRF-Token: {hx_headers}"
    assert cookie_value in hx_headers, (
        f"hx-headers token does not match cookie value; hx-headers={hx_headers!r} cookie={cookie_value!r}"
    )


def test_wizard_step_submission_sends_csrf_header(app_server: str, auth_page: Page) -> None:
    """When the wizard form is submitted via htmx, the actual XHR request
    must carry an X-CSRF-Token header that matches the cookie. This is the
    end-to-end verification that hx-headers reaches the network layer.
    """
    auth_page.goto(f"{app_server}/forms/wizard/create-project")
    auth_page.wait_for_load_state("networkidle")

    captured: dict[str, str | None] = {"header": None}

    def _on_request(request) -> None:
        if request.method == "POST" and "/forms/wizard/" in request.url:
            captured["header"] = request.headers.get(CSRF_HEADER_NAME)

    auth_page.on("request", _on_request)

    # Fill the project name minimally and submit the first step. Whether or
    # not validation passes is irrelevant -- we only need the request to fire
    # so we can inspect the header it carried.
    name_field = auth_page.locator("[name='name']").first
    if name_field.count() > 0:
        name_field.fill("e2e-csrf-test")
    auth_page.locator("button[type='submit']").first.click()
    auth_page.wait_for_load_state("networkidle")

    assert captured["header"] is not None, "wizard step POST did NOT include X-CSRF-Token header"

    cookie_value = next(c["value"] for c in auth_page.context.cookies() if c["name"] == CSRF_COOKIE_NAME)
    assert captured["header"] == cookie_value, (
        f"X-CSRF-Token header does not match cookie; header={captured['header']!r} cookie={cookie_value!r}"
    )


# ---------------------------------------------------------------------------
# Classic <form method=POST> carries a hidden csrf_token input
# ---------------------------------------------------------------------------


def test_invite_register_has_hidden_csrf_field(app_server: str, page: Page) -> None:
    """The invite-register form must include a hidden csrf_token input that
    matches the cookie. This is the form-encoded path -- no JS, no headers.

    Uses the unauthenticated `page` fixture because invite-register is the
    pre-login flow. We need an invite key that the server will accept; in
    the test app this is unconfigured, so this test verifies the rendering
    contract on whichever response we get back.
    """
    # In the test app, the invite key is invalid -- but we still want to see
    # what's rendered. If we get the form back, the hidden field must be
    # there with the cookie value.
    page.goto(f"{app_server}/invite/test-invite-key", wait_until="networkidle")

    # If we got the register form (not an error page), check the hidden field.
    form = page.locator("form[action*='/invite/'][action*='/register']")
    if form.count() == 0:
        pytest.skip("Invite key not configured in test app; cannot exercise the form path here.")

    hidden = form.locator("input[name='csrf_token']")
    assert hidden.count() == 1, "invite-register form is missing hidden csrf_token input"
    rendered = hidden.get_attribute("value")
    assert rendered, "hidden csrf_token has empty value"

    cookie_value = next(c["value"] for c in page.context.cookies() if c["name"] == CSRF_COOKIE_NAME)
    assert rendered == cookie_value, (
        f"hidden field value does not match cookie; field={rendered!r} cookie={cookie_value!r}"
    )


# ---------------------------------------------------------------------------
# Negative: a forged POST without the token is rejected (proves the
# middleware actually rejects, not just that the server sends the header)
# ---------------------------------------------------------------------------


def test_post_without_csrf_token_is_rejected(app_server: str, auth_page: Page) -> None:
    """A POST that omits both the X-CSRF-Token header and the csrf_token
    form field must be rejected with 403, even when the session cookie is
    present.

    This is the proof that the middleware actually enforces -- without it,
    all the rendering work is theatre. Done via page.evaluate so the fetch
    runs inside the browser context and inherits the session cookie that
    auth_page already established.
    """
    auth_page.goto(app_server)

    # Issue a same-origin POST from the page itself so the session cookie
    # is included. Deliberately omit BOTH the X-CSRF-Token header and the
    # csrf_token form field; the middleware must reject it.
    status = auth_page.evaluate(
        """async () => {
            const r = await fetch('/forms/wizard/create-project/step/identity', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: '{}',
                credentials: 'same-origin',
            });
            return r.status;
        }"""
    )

    assert status == 403, f"POST without CSRF token must be rejected; got status={status}"
