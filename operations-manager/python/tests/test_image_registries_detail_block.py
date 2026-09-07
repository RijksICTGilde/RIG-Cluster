"""Het blok op de projectpagina, en het endpoint dat zijn toestand ophaalt.

Twee dingen die uit elkaar horen: wat in het PROJECTBESTAND staat (de invoer van de
afnemer) komt uit de synchrone haak, en wat in het CLUSTER staat (is de proxy klaar,
wanneer verloopt het token) wordt lazy opgehaald -- want een blok dat rendert mag geen
connector aanroepen.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from opi.services.catalog.base import ProjectPageContext
from opi.services.catalog.image_registries import ImageRegistriesService
from opi.services.catalog.image_registries.web import _organization_status
from opi.services.services_enums import UIEvent

REGISTRY = {
    "name": "code-overheid",
    "upstream": "code.overheid.nl/robbert.uittenbroek",
    "username": "robbert.uittenbroek",
    "password": "-----BEGIN AGE ENCRYPTED FILE-----",
}


def _ctx(registries: list[dict[str, Any]] | None) -> ProjectPageContext:
    services: list[Any] = ["publish-on-web"]
    if registries is not None:
        services.append({"name": "image-registries", "config": {"registries": registries}})
    return ProjectPageContext(project_data={"name": "demo", "services": services}, user_role="admin")


class TestHetBlok:
    def test_zonder_registries_geen_blok(self) -> None:
        assert ImageRegistriesService().handle_ui(UIEvent.PROJECT_SECTIONS, _ctx(None)) == []

    def test_met_registries_een_blok_met_de_invoer(self) -> None:
        (section,) = ImageRegistriesService().handle_ui(UIEvent.PROJECT_SECTIONS, _ctx([REGISTRY]))
        assert section.template == "image_registries/section-detail.html.j2"
        assert section.context["registries"][0]["upstream"] == "code.overheid.nl/robbert.uittenbroek"
        assert section.context["project_name"] == "demo"

    def test_het_blok_rekent_geen_rcr_url_uit(self) -> None:
        """Die is een BEREKENING; hem hier neerzetten zou een tweede waarheid geven."""
        (section,) = ImageRegistriesService().handle_ui(UIEvent.PROJECT_SECTIONS, _ctx([REGISTRY]))
        assert "rcr" not in str(section.context["registries"])

    def test_de_dienst_brengt_zijn_eigen_endpoint_mee(self) -> None:
        """Een blok dat lazy laadt bezit de route die het vult; anders blijft de helft
        achter in de algemene router."""
        from opi.services.catalog.image_registries.web import image_registries_router

        assert image_registries_router in ImageRegistriesService().web_routers()


def _kubectl(stdout: str, code: int = 0) -> Any:
    connector = AsyncMock()
    connector.run_command = AsyncMock(return_value=(stdout, "", code))
    return connector


@pytest.mark.asyncio
class TestDeToestandUitHetCluster:
    async def test_klaar_als_de_proxy_en_de_credentials_er_zijn(self) -> None:
        status = await _organization_status(
            _kubectl('{"status": {"proxyCache": {"ready": true}, "credentialsConfigured": true}}'),
            "rig-prd-demo",
            "code-overheid",
            "code.overheid.nl/robbert.uittenbroek",
            "rig",
            "demo",
        )
        assert status["state"] == "ready"
        assert status["organization"] == "codeoverheid-rig-demo"

    async def test_de_proxy_draait_maar_de_credentials_nog_niet(self) -> None:
        status = await _organization_status(
            _kubectl('{"status": {"proxyCache": {"ready": true}, "credentialsConfigured": false}}'),
            "rig-prd-demo",
            "code-overheid",
            "code.overheid.nl/x",
            "rig",
            "demo",
        )
        assert status["state"] == "pending"
        assert "inloggegevens" in status["message"]

    async def test_een_organisatie_die_er_nog_niet_is_is_geen_fout(self) -> None:
        """20 tot 25 seconden, gemeten. Een pod die te vroeg start herstelt vanzelf."""
        status = await _organization_status(
            _kubectl("", code=1), "rig-prd-demo", "code-overheid", "code.overheid.nl/x", "rig", "demo"
        )
        assert status["state"] == "pending"
        assert "herstelt vanzelf" in status["message"]

    async def test_de_verloopdatum_van_het_token_komt_mee(self) -> None:
        """Het token roteert elke 90 dagen; wie dat niet ziet aankomen staat stil."""
        status = await _organization_status(
            _kubectl(
                '{"status": {"proxyCache": {"ready": true}, "credentialsConfigured": true,'
                ' "tokenExpiryDate": "2026-12-06T00:00:00Z"}}'
            ),
            "rig-prd-demo",
            "code-overheid",
            "code.overheid.nl/x",
            "rig",
            "demo",
        )
        assert status["expires_at"].startswith("2026-12-06")

    async def test_onleesbare_status_geeft_geen_uitspraak(self) -> None:
        status = await _organization_status(
            _kubectl("geen json"), "rig-prd-demo", "code-overheid", "code.overheid.nl/x", "rig", "demo"
        )
        assert status["state"] == "unknown"

    async def test_hij_vraagt_de_organisatie_op_zijn_berekende_naam(self) -> None:
        kubectl = _kubectl('{"status": {}}')
        await _organization_status(kubectl, "rig-prd-demo", "code-overheid", "code.overheid.nl/robbert", "rig", "demo")
        kubectl.run_command.assert_awaited_once_with(
            ["get", "organization", "codeoverheid-rig-demo", "-n", "rig-prd-demo", "-o", "json"]
        )
