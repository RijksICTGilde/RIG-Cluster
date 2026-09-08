"""Het blok op de projectpagina, en het endpoint dat zijn toestand uit het cluster haalt."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from fastapi.responses import HTMLResponse
from opi.services.catalog.base import ProjectPageContext
from opi.services.catalog.image_registries import ImageRegistriesService
from opi.services.catalog.image_registries.naming import organization_name
from opi.services.catalog.image_registries.web import _organization_status, registry_status_fragment
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
        assert status["organization"] == organization_name("code.overheid.nl/robbert.uittenbroek", "rig", "demo")

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
        organisatie = organization_name("code.overheid.nl/robbert", "rig", "demo")
        kubectl.run_command.assert_awaited_once_with(
            ["get", "organization", organisatie, "-n", "rig-prd-demo", "-o", "json"]
        )


class TestDeVerloopwaarschuwing:
    """Het token roteert elke 90 dagen en niemand ververst het uit zichzelf, dus de afnemer
    moet het zien aankomen terwijl er nog tijd is om er iets aan te doen."""

    @staticmethod
    def _over(days: int) -> str:
        from datetime import UTC, datetime, timedelta

        return (datetime.now(UTC) + timedelta(days=days)).isoformat().replace("+00:00", "Z")

    def test_binnen_veertien_dagen_is_dringend(self) -> None:
        from opi.services.catalog.image_registries.web import _expires_soon

        assert _expires_soon(self._over(3)) is True

    def test_al_verlopen_is_ook_dringend(self) -> None:
        from opi.services.catalog.image_registries.web import _expires_soon

        assert _expires_soon(self._over(-1)) is True

    def test_ruim_op_tijd_is_geen_waarschuwing(self) -> None:
        from opi.services.catalog.image_registries.web import _expires_soon

        assert _expires_soon(self._over(60)) is False

    def test_geen_datum_is_geen_waarschuwing(self) -> None:
        from opi.services.catalog.image_registries.web import _expires_soon

        assert _expires_soon("") is False

    def test_een_onleesbare_datum_is_geen_waarschuwing(self) -> None:
        """Dringend melden op een aanname is erger dan zwijgen over iets wat misschien
        niets is."""
        from opi.services.catalog.image_registries.web import _expires_soon

        assert _expires_soon("morgen") is False

    async def test_de_vlag_komt_mee_uit_het_cluster(self) -> None:
        import json

        from opi.services.catalog.image_registries.web import _organization_status

        status = await _organization_status(
            _kubectl(
                json.dumps(
                    {
                        "status": {
                            "proxyCache": {"ready": True},
                            "credentialsConfigured": True,
                            "tokenExpiryDate": self._over(2),
                        }
                    }
                )
            ),
            "rig-prd-demo",
            "code-overheid",
            "code.overheid.nl/x",
            "rig",
            "demo",
        )
        assert status["expires_soon"] is True


@pytest.mark.asyncio
class TestDeLeeswegVanHetStatusEndpoint:
    """Welke weg de handler naar het projectbestand neemt.

    Het fragment gebruikt uit elke registry precies twee tekstvelden, ``name`` en
    ``upstream``. ``get_decrypted()`` zou daarvoor ``decrypt_tree()`` over de hele boom
    draaien (de AGE-privesleutel, de api-key, de user-env-vars en het registry-token)
    en die waarden komen nergens in dit antwoord terecht. ``get()`` levert hetzelfde
    antwoord zonder ze aan te raken.
    """

    def _patch(self, monkeypatch: pytest.MonkeyPatch, registries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        import opi.connectors.kubectl as kubectl_module
        import opi.core.cluster_config as cluster_config
        import opi.services.project_authorization as authorization
        import opi.services.project_store as project_store
        import opi.web.lotc_switch as lotc_switch
        from opi.services.catalog.image_registries.resolution import BACKEND_QUAY_PROXY

        async def _geen_ontsleuteling(name: str) -> None:
            raise AssertionError("het statusfragment hoeft het projectbestand niet ontsleuteld")

        project = SimpleNamespace(
            data={"name": "demo", "services": [{"name": "image-registries", "config": {"registries": registries}}]}
        )
        store = SimpleNamespace(get=lambda name: project, get_decrypted=_geen_ontsleuteling)

        monkeypatch.setattr(project_store, "get_project_store", lambda: store)
        monkeypatch.setattr(authorization, "is_user_authorized_for_project", lambda project_name, email: True)
        monkeypatch.setattr(
            cluster_config,
            "get_image_registries_config",
            lambda cluster: {"backend": BACKEND_QUAY_PROXY, "customer_name": "rig"},
        )
        monkeypatch.setattr(cluster_config, "get_prefixed_namespace", lambda cluster, project_name: "rig-prd-demo")
        monkeypatch.setattr(
            kubectl_module,
            "KubectlConnector",
            lambda: _kubectl('{"status": {"proxyCache": {"ready": true}, "credentialsConfigured": true}}'),
        )

        gerenderd: list[dict[str, Any]] = []

        def _render(request: Any, *, template: str, context: dict[str, Any]) -> Any:
            gerenderd.append(context)
            return HTMLResponse("")

        monkeypatch.setattr(lotc_switch, "render", _render)
        return gerenderd

    def _request(self) -> Any:
        return SimpleNamespace(state=SimpleNamespace(user={"email": "admin@rijksoverheid.nl"}))

    async def test_het_fragment_leest_het_bestand_zonder_te_ontsleutelen(self, monkeypatch: pytest.MonkeyPatch) -> None:
        gerenderd = self._patch(monkeypatch, [REGISTRY])

        await registry_status_fragment(self._request(), "demo")

        (context,) = gerenderd
        assert context["applicable"] is True
        assert [status["registry"] for status in context["statuses"]] == ["code-overheid"]
        assert context["statuses"][0]["state"] == "ready"

    async def test_zonder_registries_wordt_het_cluster_niet_bevraagd(self, monkeypatch: pytest.MonkeyPatch) -> None:
        gerenderd = self._patch(monkeypatch, [])

        await registry_status_fragment(self._request(), "demo")

        assert gerenderd == [{"applicable": True, "statuses": []}]

    async def test_wie_geen_lid_is_krijgt_403_en_ziet_niets(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """De weigering van het endpoint zelf. Zonder deze test is de eigendomscontrole
        ongedekt: hem weghalen laat de rest van de suite groen, terwijl het fragment dan de
        namen en upstreams van andermans registries teruggeeft."""
        import opi.services.project_authorization as authorization

        gerenderd = self._patch(monkeypatch, [REGISTRY])
        monkeypatch.setattr(authorization, "is_user_authorized_for_project", lambda project_name, email: False)

        with pytest.raises(HTTPException) as opgevangen:
            await registry_status_fragment(self._request(), "demo")

        assert opgevangen.value.status_code == 403
        assert gerenderd == []
