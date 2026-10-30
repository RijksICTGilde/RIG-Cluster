"""The Configuratie & Secrets block on the project page is for platform admins only.

It held the old project API key (which carries platform admin roles) and the AGE keys,
and was shown to anyone with ``project.edit``. Members now issue their own tokens under
Leden en tokens, so the block is management information.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from opi.core.templates_lotc import templates_lotc as templates
from opi.web.lotc_fixtures import page_data

KOP = "Configuratie &amp; Secrets"
MELDING = "Je ziet dit blok omdat je platformbeheerder bent."


def _render(is_platform_admin: bool) -> str:
    request = SimpleNamespace(
        scope={"type": "http"},
        headers={},
        cookies={},
        state=SimpleNamespace(),
        url=SimpleNamespace(path="/"),
        session={},
        query_params={},
    )
    data = page_data("project-tabs")
    data["project"] = {**data["project"], "is_platform_admin": is_platform_admin}
    data["active_tab"] = "project"
    data["tabs"] = {sleutel: {**tab, "url": "#"} for sleutel, tab in data["tabs"].items()}
    return templates.env.get_template("bg/project-tabs.html.j2").render(request=request, navigation=[], **data)


@pytest.mark.parametrize("is_platform_admin", [True, False])
def test_alleen_een_platformbeheerder_ziet_het_blok(is_platform_admin: bool) -> None:
    html = _render(is_platform_admin)
    assert (KOP in html) is is_platform_admin
    assert (MELDING in html) is is_platform_admin
