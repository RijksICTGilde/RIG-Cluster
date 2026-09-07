"""De tokentoets bij het opslaan (D5).

Een te smal token wordt upstream met ``reqPackageAccess`` (401) beantwoord, Quay vertaalt
dat naar ``name unknown: repository not found``, en de afnemer ziet ImagePullBackOff met
een melding die de verkeerde kant op wijst. Deze toets is de plek waar hij wel een
bruikbare fout krijgt.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from opi.forms.editables.enforcers import FieldError
from opi.services.catalog.image_registries.enforcers import RegistryTokenEnforcer, _repository_under

REGISTRY = {
    "name": "code-overheid",
    "upstream": "code.overheid.nl/robbert.uittenbroek",
    "username": "robbert.uittenbroek",
    "password": "een-token",
}
IMAGE = "code.overheid.nl/robbert.uittenbroek/zad-deployment-demo:0a611d9d"


def _data(registries: list[dict[str, Any]], images: list[str] | None = None) -> dict[str, Any]:
    return {
        "name": "demo",
        "services": [{"name": "image-registries", "config": {"registries": registries}}],
        "components": [{"name": "web", "image": image} for image in (images or [])],
    }


def _connector(ok: bool, reason: str = "") -> Any:
    connector = AsyncMock()
    connector.check_repository_access = AsyncMock(return_value=(ok, reason))
    return connector


class TestDeRepositoryDieGetoetstWordt:
    def test_de_tag_valt_eraf(self) -> None:
        """list-tags VRAAGT naar de tags; er een meegeven maakt de vraag onbeantwoordbaar."""
        assert (
            _repository_under("code.overheid.nl/robbert.uittenbroek", [IMAGE])
            == "code.overheid.nl/robbert.uittenbroek/zad-deployment-demo"
        )

    def test_een_digest_valt_er_ook_af(self) -> None:
        image = "code.overheid.nl/robbert.uittenbroek/demo@sha256:abc"
        assert (
            _repository_under("code.overheid.nl/robbert.uittenbroek", [image])
            == "code.overheid.nl/robbert.uittenbroek/demo"
        )

    def test_een_image_van_een_andere_registry_telt_niet_mee(self) -> None:
        assert _repository_under("code.overheid.nl/robbert.uittenbroek", ["ghcr.io/x/y:1"]) is None

    def test_alleen_op_segmentgrens(self) -> None:
        assert _repository_under("code.overheid.nl/robbert", ["code.overheid.nl/robbertx/app:1"]) is None


@pytest.mark.asyncio
class TestDeToets:
    async def test_een_te_smal_token_geeft_een_fout_op_het_tokenveld(self) -> None:
        data = _data([REGISTRY], [IMAGE])
        with (
            patch(
                "opi.services.catalog.image_registries.enforcers._connector",
                return_value=_connector(False, "unauthorized: reqPackageAccess"),
            ),
            pytest.raises(FieldError) as exc,
        ):
            await RegistryTokenEnforcer().enforce(data, {"project_name": "demo"})
        assert exc.value.field_path == "services/image-registries/config/registries[0]/password"
        assert "leesrecht op packages" in str(exc.value)
        assert "reqPackageAccess" in str(exc.value)

    async def test_een_goed_token_gaat_door(self) -> None:
        data = _data([REGISTRY], [IMAGE])
        with patch("opi.services.catalog.image_registries.enforcers._connector", return_value=_connector(True)):
            assert await RegistryTokenEnforcer().enforce(data, {"project_name": "demo"}) is data

    async def test_zonder_image_wordt_er_niets_geweigerd(self) -> None:
        """De normale toestand in de wizard: de registry komt voor de componenten. Een
        weigering op iets wat we niet gemeten hebben zou een gebruiker blokkeren op een
        aanname."""
        connector = _connector(False, "zou niet aangeroepen mogen worden")
        data = _data([REGISTRY])
        with patch("opi.services.catalog.image_registries.enforcers._connector", return_value=connector):
            await RegistryTokenEnforcer().enforce(data, {"project_name": "demo"})
        connector.check_repository_access.assert_not_awaited()

    async def test_zonder_inloggegevens_wordt_er_niets_getoetst(self) -> None:
        connector = _connector(False)
        data = _data([{"name": "publiek", "upstream": "code.overheid.nl/open"}], [IMAGE])
        with patch("opi.services.catalog.image_registries.enforcers._connector", return_value=connector):
            await RegistryTokenEnforcer().enforce(data, {"project_name": "demo"})
        connector.check_repository_access.assert_not_awaited()

    async def test_de_juiste_repository_en_inloggegevens_gaan_naar_de_connector(self) -> None:
        connector = _connector(True)
        data = _data([REGISTRY], [IMAGE])
        with patch("opi.services.catalog.image_registries.enforcers._connector", return_value=connector):
            await RegistryTokenEnforcer().enforce(data, {"project_name": "demo"})
        connector.check_repository_access.assert_awaited_once_with(
            "code.overheid.nl/robbert.uittenbroek/zad-deployment-demo", "robbert.uittenbroek", "een-token"
        )

    async def test_zonder_connector_wordt_er_niets_geweigerd(self) -> None:
        """Geen skopeo is geen oordeel over het token."""
        data = _data([REGISTRY], [IMAGE])
        with patch("opi.services.catalog.image_registries.enforcers._connector", return_value=None):
            await RegistryTokenEnforcer().enforce(data, {"project_name": "demo"})

    async def test_de_tweede_registry_wordt_ook_gemeten(self) -> None:
        """De fout wijst het VELD aan, dus de index moet die van de echte registry zijn."""
        ander = {**REGISTRY, "name": "ander", "upstream": "ghcr.io/team"}
        data = _data([ander, REGISTRY], [IMAGE])
        with (
            patch("opi.services.catalog.image_registries.enforcers._connector", return_value=_connector(False, "401")),
            pytest.raises(FieldError) as exc,
        ):
            await RegistryTokenEnforcer().enforce(data, {"project_name": "demo"})
        assert exc.value.field_path.endswith("registries[1]/password")


class TestDeConnector:
    """De maskering, want een foutregel van skopeo kan de verwijzing mét token bevatten."""

    def test_de_creds_worden_gemaskeerd_in_het_log(self) -> None:
        from opi.connectors.skopeo import SkopeoConnector

        masked = SkopeoConnector._mask_list_tags_credentials(
            ["skopeo", "list-tags", "--creds", "robbert:geheim", "docker://x"]
        )
        assert masked[3] == "robbert:***"
        assert "geheim" not in " ".join(masked)

    def test_een_userinfo_in_een_foutmelding_wordt_gemaskeerd(self) -> None:
        from opi.connectors.skopeo import SkopeoConnector

        masked = SkopeoConnector._mask_userinfo("error pinging docker://robbert:geheim@code.overheid.nl/v2/")
        assert "geheim" not in masked
        assert "***@code.overheid.nl" in masked
