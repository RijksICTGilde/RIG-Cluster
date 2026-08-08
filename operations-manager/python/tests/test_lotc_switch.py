"""De schakelaar moet terugvallen als de bouwlijn er niet is.

Lord of the Components zit in een dependency-group en niet in de runtime-dependencies,
dus in de release-image bestaat het pakket niet. Een gedeelde link met ``?ui=lotc`` komt
daar vroeg of laat terecht, en dan hoort hij de bestaande pagina te tonen - geen 500.

Dat is precies het soort fout dat pas op de sandbox of in productie opvalt, want in
ontwikkeling is het pakket er altijd.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest


def _request(query: str) -> SimpleNamespace:
    """Het kleinste dat de schakelaar van een verzoek nodig heeft."""
    return SimpleNamespace(query_params={"ui": query} if query else {})


def test_wants_lotc_only_on_the_exact_value() -> None:
    from opi.web.lotc_switch import wants_lotc

    assert wants_lotc(_request("lotc"))
    assert not wants_lotc(_request(""))
    assert not wants_lotc(_request("iets-anders"))


def test_falls_back_to_the_existing_page_without_the_build_line() -> None:
    """Zonder LOTC geinstalleerd rendert ?ui=lotc de bestaande template."""
    pytest.importorskip("lord_of_the_components", reason="LOTC-bouwlijn niet geinstalleerd")

    from opi.web import lotc_switch

    gerenderd: dict[str, str] = {}

    class _Templates:
        def TemplateResponse(self, request, name, context):
            gerenderd["template"] = name
            return "gerenderd"

    real_import = __import__

    def zonder_lotc(name, *args, **kwargs):
        if name == "opi.core.templates_lotc":
            raise ImportError("gesimuleerd: de bouwlijn is niet geinstalleerd")
        return real_import(name, *args, **kwargs)

    with (
        patch("builtins.__import__", side_effect=zonder_lotc),
        patch("opi.core.templates.setup_templates", return_value=_Templates()),
    ):
        lotc_switch.render(_request("lotc"), roos="oud.html.j2", lotc="nieuw.html.j2", context={})

    assert gerenderd["template"] == "oud.html.j2", "viel niet terug op de bestaande pagina"
