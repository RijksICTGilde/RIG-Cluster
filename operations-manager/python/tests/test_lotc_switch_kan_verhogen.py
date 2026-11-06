"""``render()`` zet ``kan_verhogen``: toont het accountmenu de knop 'Verhoogde rechten' (RC-431).

Los van ``verhoogd_tot`` (de HUIDIGE status): ``kan_verhogen`` zegt of dit account
platformbeheerder is en de knop dus MAG zien, wie dan ook de elevatie nog moet starten.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from opi.core.templates_lotc import templates_lotc as echte_templates
from opi.web.lotc_switch import render
from starlette.requests import Request


def _request(email: str) -> Request:
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    request.state.user = {"email": email}
    return request


def _context_van(email: str, *, platform_admin: bool) -> dict[str, Any]:
    users = MagicMock(is_platform_admin=MagicMock(return_value=platform_admin))
    gevangen: dict[str, Any] = {}

    def _vang(_request: Request, _template: str, context: dict[str, Any], status_code: int = 200) -> MagicMock:
        gevangen.update(context)
        return MagicMock()

    with (
        patch("opi.web.lotc_switch.get_user_service", return_value=users),
        patch.object(echte_templates, "TemplateResponse", side_effect=_vang),
    ):
        render(_request(email), template="irrelevant.html.j2", context={})
    return gevangen


def test_a_platform_admin_can_elevate() -> None:
    context = _context_van("beheer@rijksoverheid.nl", platform_admin=True)

    assert context["kan_verhogen"] is True


def test_an_ordinary_user_cannot_elevate() -> None:
    context = _context_van("lid@rijksoverheid.nl", platform_admin=False)

    assert context["kan_verhogen"] is False
