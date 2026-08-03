"""Tests for the on-demand realm-admin OTP code endpoint.

The endpoint exists so the shared seed stays on the server: the detail page used
to render the seed itself (Base32 + otpauth URI + a QR), which hands anyone who
can see the page the ability to generate codes forever. These tests pin the two
properties that buys us: the seed never reaches a response, and the endpoint
repeats the admin/owner gate of the section that hosts the button.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from opi.web.router import keycloak_otp_code_web

PROJECT = "demo"
REALM = "demo-realm"
ENCRYPTED_SEED = "AGE-ENCRYPTED-SEED-BLOB"
RAW_SEED = "12345678901234567890"

_SECTION_TEMPLATE = Path(__file__).parent.parent / "opi/services/catalog/keycloak/section-detail.html.j2"


def _project_data() -> dict[str, Any]:
    return {
        "config": {"age-private-key": "ENCRYPTED-PROJECT-KEY"},
        "services": [
            {
                "keycloak": {
                    "config": {
                        "realms": [
                            {
                                "host": "https://kc.example",
                                "realm": REALM,
                                "username": "demo_admin",
                                "password": "ENCRYPTED-PW",
                                "totp_secret": ENCRYPTED_SEED,
                            }
                        ]
                    }
                }
            }
        ],
    }


async def _call(role: str = "admin", realm: str = REALM, data: dict[str, Any] | None = None) -> tuple[Any, dict]:
    """Invoke the endpoint with the surrounding lookups stubbed.

    Returns the response plus the context handed to the template, which is where
    a leaked seed would show up.
    """
    captured: dict = {}

    def _template_response(request: Any, name: str, context: dict) -> str:
        captured.update({"template": name, "context": context})
        return "<html/>"

    templates = MagicMock()
    templates.TemplateResponse.side_effect = _template_response

    store = MagicMock()
    store.get.return_value = MagicMock(data=_project_data() if data is None else data)

    # decrypt is called twice: project private key, then the seed itself.
    decrypt = AsyncMock(side_effect=["PROJECT-PRIVATE-KEY", RAW_SEED])

    with (
        patch("opi.web.router.get_current_user", return_value={"email": "a@example.com"}),
        patch("opi.web.router.is_user_authorized_for_project", return_value=True),
        patch("opi.web.router.get_user_role_for_project", return_value=role),
        patch("opi.web.router.get_project_store", return_value=store),
        patch("opi.web.router.get_global_private_key", return_value="GLOBAL-KEY"),
        patch("opi.web.router.decrypt_password_smart", decrypt),
        patch("opi.web.router.get_templates", return_value=templates),
    ):
        response = await keycloak_otp_code_web(MagicMock(), PROJECT, realm)

    return response, captured


async def test_returns_a_six_digit_code_and_its_remaining_validity() -> None:
    _, captured = await _call()

    assert captured["template"] == "keycloak/otp-code.html.j2"
    code = captured["context"]["code"]
    assert len(code) == 6, code
    assert code.isdigit(), code
    assert 1 <= captured["context"]["seconds_remaining"] <= 30


async def test_the_seed_never_reaches_the_response() -> None:
    """Neither the raw nor the encrypted seed may end up in the render context."""
    _, captured = await _call()

    rendered = repr(captured["context"])
    assert RAW_SEED not in rendered
    assert ENCRYPTED_SEED not in rendered
    # Base32 of the seed is what an authenticator would ingest: equally disqualifying.
    assert "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ" not in rendered


@pytest.mark.parametrize("role", ["viewer", "member", "", None])
async def test_only_admin_and_owner_may_fetch_a_code(role: str | None) -> None:
    with pytest.raises(HTTPException) as exc:
        await _call(role=role)  # type: ignore[arg-type]
    assert exc.value.status_code == 403


async def test_unknown_realm_is_a_404_not_a_crash() -> None:
    with pytest.raises(HTTPException) as exc:
        await _call(realm="does-not-exist")
    assert exc.value.status_code == 404


async def test_realm_without_otp_is_a_404() -> None:
    data = _project_data()
    del data["services"][0]["keycloak"]["config"]["realms"][0]["totp_secret"]

    with pytest.raises(HTTPException) as exc:
        await _call(data=data)
    assert exc.value.status_code == 404


def test_detail_section_no_longer_renders_the_seed_or_loads_a_cdn_script() -> None:
    """Drift guard on the template that hosts the button.

    The seed used to be rendered here, next to a jsdelivr script tag that could
    read it. Both are gone; this fails if either creeps back.
    """
    source = _SECTION_TEMPLATE.read_text()

    assert "totp_secret" not in source
    assert "totp_otpauth_uri" not in source
    assert "cdn.jsdelivr.net" not in source
    assert "otp-code" in source, "the on-demand code button should still be here"
