"""De beheerpagina Toegang (/admin/toegang).

Twee dingen worden hier vastgehouden.

DE GRENDEL. Op deze pagina staan de wachtwoorden van Keycloak, Forgejo en ArgoCD bij
elkaar. Het menu-item is alleen voor een beheerder zichtbaar, maar een link verbergen is
presentatie en geen toegangscontrole: de URL is de weg naar binnen. Er is bewust geen
fragment naast de pagina, want dat zou een tweede URL zijn waar dezelfde wachtwoorden
uitkomen.

WAT ER IN DE LIJST KOMT. Een dienst waarvan het geheim niet bestaat hoort te verdwijnen en
niet als lege regel te blijven staan, en een dienst waarvan het geheim er wel is maar het
wachtwoordveld niet hoort zichtbaar te blijven met een melding. Dat verschil is precies wat
een beheerder moet kunnen zien: "deze dienst draait hier niet" is iets anders dan "ik kan
er niet in".
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from opi.services.platform_toegang import DIENSTEN, haal_toegang
from opi.web.router_toegang import toegang_overzicht


def _verzoek(email: str | None) -> Any:
    user = {"email": email} if email else None
    return SimpleNamespace(state=SimpleNamespace(user=user), query_params={})


def _gebruikersdienst(admins: set[str]) -> Any:
    dienst = MagicMock()
    dienst.is_platform_admin.side_effect = lambda email: email in admins
    return dienst


class TestDeGrendel:
    @pytest.mark.asyncio
    async def test_een_niet_beheerder_komt_er_niet_in(self) -> None:
        with (
            patch("opi.services.user_service.get_user_service", return_value=_gebruikersdienst(set())),
            pytest.raises(HTTPException) as fout,
        ):
            await toegang_overzicht(_verzoek("ontwikkelaar@rijksoverheid.nl"))

        assert fout.value.status_code == 403

    @pytest.mark.asyncio
    async def test_zonder_sessie_komt_er_niemand_in(self) -> None:
        with (
            patch("opi.services.user_service.get_user_service", return_value=_gebruikersdienst(set())),
            pytest.raises(HTTPException) as fout,
        ):
            await toegang_overzicht(_verzoek(None))

        assert fout.value.status_code == 401

    @pytest.mark.asyncio
    async def test_een_beheerder_komt_langs_de_grendel_en_krijgt_no_store(self) -> None:
        """De grendel houdt een beheerder niet tegen, en het antwoord mag niet bewaard worden.

        ``no-store`` en niet ``no-cache``: dat laatste laat opslaan nog steeds toe en vraagt
        alleen om hervalidatie, en dan staan de wachtwoorden alsnog op schijf.
        """
        antwoord_mock = MagicMock()
        antwoord_mock.headers = {}
        with (
            patch(
                "opi.services.user_service.get_user_service",
                return_value=_gebruikersdienst({"beheerder@rijksoverheid.nl"}),
            ),
            patch("opi.web.router_toegang.render", return_value=antwoord_mock) as render,
            patch("opi.web.router_toegang.get_menu_items", return_value=[]),
            patch("opi.web.router_toegang.build_lotc_admin", return_value={}),
            patch("opi.web.router_toegang.haal_toegang", AsyncMock(return_value=[])),
        ):
            antwoord = await toegang_overzicht(_verzoek("beheerder@rijksoverheid.nl"))

        assert render.called
        assert antwoord.headers["Cache-Control"] == "no-store"


class TestWatErInDeLijstKomt:
    """De lijst volgt het cluster en niet een vaste opsomming."""

    @staticmethod
    def _connector(geheimen: dict[str, dict[str, str]], hosts: list[dict[str, Any]]) -> Any:
        connector = MagicMock()
        connector.get_secret = AsyncMock(side_effect=lambda naam, _ns: geheimen.get(naam))
        connector.get_resources_by_label = AsyncMock(return_value=hosts)
        return connector

    @staticmethod
    def _ingress(host: str) -> dict[str, Any]:
        return {"spec": {"rules": [{"host": host}]}}

    @pytest.mark.asyncio
    async def test_een_dienst_zonder_geheim_verdwijnt(self) -> None:
        """Geen dode regel op een cluster dat die dienst niet draait.

        Dezelfde vorm als ``has_mail_relay``: de omgeving beantwoordt de
        beschikbaarheidsvraag.
        """
        connector = self._connector(
            geheimen={"argocd-cluster": {"admin.password": "geheim"}},
            hosts=[self._ingress("argo.voorbeeld.nl")],
        )
        with patch("opi.services.platform_toegang.create_kubectl_connector", return_value=connector):
            regels = await haal_toegang()

        assert [r.naam for r in regels] == ["ArgoCD"]
        assert regels[0].gebruiker == "admin"
        assert regels[0].wachtwoord == "geheim"

    @pytest.mark.asyncio
    async def test_een_geheim_zonder_wachtwoordveld_blijft_zichtbaar(self) -> None:
        """ "Ik kan er niet in" is iets anders dan "die draait hier niet"."""
        connector = self._connector(
            geheimen={"argocd-cluster": {"iets-anders": "x"}},
            hosts=[self._ingress("argo.voorbeeld.nl")],
        )
        with patch("opi.services.platform_toegang.create_kubectl_connector", return_value=connector):
            regels = await haal_toegang()

        assert [r.naam for r in regels] == ["ArgoCD"]
        assert regels[0].wachtwoord == ""

    @pytest.mark.asyncio
    async def test_het_adres_komt_uit_de_ingress(self) -> None:
        """De Ingress is de waarheid, want die volgt een domeinwijziging vanzelf."""
        connector = self._connector(
            geheimen={"forgejo-admin": {"username": "rig-admin", "password": "geheim"}},
            hosts=[self._ingress("forgejo.ergens-anders.nl")],
        )
        with patch("opi.services.platform_toegang.create_kubectl_connector", return_value=connector):
            regels = await haal_toegang()

        assert regels[0].url == "https://forgejo.ergens-anders.nl"
        assert "loopt achter" in regels[0].waarschuwing

    @pytest.mark.asyncio
    async def test_zonder_ingress_valt_hij_terug_en_zegt_dat(self) -> None:
        connector = self._connector(
            geheimen={"forgejo-admin": {"username": "rig-admin", "password": "geheim"}},
            hosts=[],
        )
        with patch("opi.services.platform_toegang.create_kubectl_connector", return_value=connector):
            regels = await haal_toegang()

        assert regels[0].url.startswith("https://forgejo.")
        assert "geen Ingress" in regels[0].waarschuwing

    @pytest.mark.asyncio
    async def test_een_kapotte_dienst_neemt_de_andere_niet_mee(self) -> None:
        """De beheerder die hier komt heeft meestal aan een van de drie genoeg."""
        connector = MagicMock()

        async def _lees(naam: str, _ns: str) -> dict[str, str] | None:
            if naam == "keycloak-admin-credentials":
                raise RuntimeError("kubectl viel om")
            if naam == "argocd-cluster":
                return {"admin.password": "geheim"}
            return None

        connector.get_secret = AsyncMock(side_effect=_lees)
        connector.get_resources_by_label = AsyncMock(return_value=[])
        with patch("opi.services.platform_toegang.create_kubectl_connector", return_value=connector):
            regels = await haal_toegang()

        assert [r.naam for r in regels] == ["ArgoCD"]

    def test_geen_machine_naar_machine_geheimen_in_de_lijst(self) -> None:
        """De lijst is kort met opzet.

        Databaserollen, Redis, het metrics-token, de relay-admin en chisel zijn koppelingen
        tussen componenten; die genereer je opnieuw als je ze kwijt bent. Ze hier zetten
        maakt de lijst lang en daarmee de regels die er wel toe doen onvindbaar.
        """
        verboden = {
            "postgres-admin-credentials",
            "redis-admin-credentials",
            "prometheus-metrics-auth",
            "mail-relay-credentials",
            "chisel-auth-credentials",
            "forgejo-db-credentials",
            "keycloak-db-credentials",
            "mail-db-credentials",
        }
        assert {d.secret_naam for d in DIENSTEN}.isdisjoint(verboden)

    def test_argocd_leest_niet_uit_de_bcrypt_blauwdruk(self) -> None:
        """De operator zet de PLATTE TEKST in argocd-cluster.

        Onze eigen blauwdruk argocd-admin-secret.yaml maakt een bcrypt-hash, en daar valt
        niets uit terug te lezen. Wijst deze bron ooit naar argocd-admin-credentials, dan
        toont de pagina een hash alsof het een wachtwoord is.
        """
        argocd = next(d for d in DIENSTEN if d.naam == "ArgoCD")
        assert argocd.secret_naam == "argocd-cluster"
        assert argocd.wachtwoord_veld == "admin.password"


class TestHetSjabloon:
    """Het sjabloon moet renderen, en de ROOS-valkuilen moeten eruit blijven.

    Dit is geen overbodige test. ROOS hergeeft attribuutwaarden in dubbele quotes, waardoor
    JSON of haakjes in een attribuut breken, en ``<c-button>`` laat geen ``onclick`` als los
    attribuut toe. Allebei geven pas bij het renderen een fout, en dat is te laat als de
    pagina alleen door een browsertest gedekt wordt.
    """

    TEMPLATE = "bg/_toegang-diensten.html.j2"

    @staticmethod
    def _regel(**overrides: Any) -> Any:
        from opi.services.platform_toegang import ToegangRegel

        basis: dict[str, Any] = {
            "naam": "Keycloak",
            "icoon": "shield-check-mark",
            "url": "https://keycloak.voorbeeld.nl",
            "gebruiker": "admin",
            "wachtwoord": "geheimpje",
        }
        basis.update(overrides)
        return ToegangRegel(**basis)

    def _render(self, diensten: list[Any]) -> str:
        from opi.core.templates_lotc import templates_lotc as templates

        return templates.env.get_template(self.TEMPLATE).render(diensten=diensten)

    @staticmethod
    def _zichtbaar(html: str) -> dict[str, bool]:
        """Per waarde: staat hij open op het scherm of gemaskeerd achter het oogje.

        Getoetst op de GERENDERDE opmaak en niet op de attributen in het sjabloon. Een
        assertie op `revealed` in de bron zou blijven kloppen als het component die vlag
        ooit anders gaat verwerken, en dan zou de test groen blijven terwijl er een
        wachtwoord open op het scherm staat.
        """
        import re

        gevonden = re.finditer(r'class="lotc-secret__value([^"]*)"[^>]{0,300}?data-value="([^"]*)"', html, re.DOTALL)
        return {m.group(2): "is-shown" in m.group(1) for m in gevonden}

    def test_het_wachtwoord_staat_er_afgeschermd_op_en_het_adres_open(self) -> None:
        zichtbaar = self._zichtbaar(self._render([self._regel()]))

        assert zichtbaar["https://keycloak.voorbeeld.nl"] is True
        assert zichtbaar["admin"] is True
        assert zichtbaar["geheimpje"] is False

    def test_een_lege_lijst_zegt_dat_en_valt_niet_om(self) -> None:
        html = self._render([])

        assert "Geen diensten gevonden" in html

    def test_de_waarschuwing_komt_op_de_pagina(self) -> None:
        html = self._render([self._regel(waarschuwing="De Ingress wijst ergens anders heen.")])

        assert "De Ingress wijst ergens anders heen." in html


def test_de_iconen_van_de_diensten_bestaan_echt() -> None:
    """Een iconnaam die NLDD niet kent rendert LEEG, zonder foutmelding.

    Er staat al een test op iconnamen (tests/test_lotc_icon_mapping.py), maar die scant
    alleen sjablonen. De iconen van deze pagina staan in Python, in DIENSTEN, en vielen
    daarmee buiten dat net: `synchroniseren` stond er ongemerkt in en bestaat niet.
    """
    from opi.web.navigation_lotc import to_nldd_icon
    from opi.web.nldd_iconen import nldd_icon_names

    woordenschat = set(nldd_icon_names())
    onbekend = {d.naam: d.icoon for d in DIENSTEN if to_nldd_icon(d.icoon) not in woordenschat}

    assert not onbekend, f"iconen die NLDD niet kent: {onbekend}"
