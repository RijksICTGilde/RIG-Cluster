"""De tokentoets bij het opslaan."""

from __future__ import annotations

import base64
import shutil
import subprocess
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from opi.connectors.skopeo import UNREADABLE_REASON, SkopeoConnector
from opi.forms.editables.enforcers import FieldError
from opi.services.catalog.image_registries.enforcers import RegistryTokenEnforcer, _repository_under
from opi.utils.age import encrypt_age_content_sync

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

    def test_een_upstream_zonder_pad_vindt_zijn_image_ook(self) -> None:
        """De vorm die de vloot echt heeft: ``algor-odc`` noemt zijn upstream ``ghcr.io``.

        Een prefix door de IMAGE-normalisatie halen maakte er ``docker.io/library/ghcr.io``
        van, en dan matcht geen enkele image, dus de toets sloeg zwijgend over.
        """
        image = "ghcr.io/rijksictgilde/algoritmeregister/backend:2024.11.24"
        assert _repository_under("ghcr.io", [image]) == "ghcr.io/rijksictgilde/algoritmeregister/backend"


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
        assert UNREADABLE_REASON in str(exc.value)
        assert "leesrecht op packages" in str(exc.value)
        assert "reqPackageAccess" not in str(exc.value)

    async def test_een_geweigerde_bestemming_geeft_alleen_de_vaste_melding_op_de_upstream(self) -> None:
        registry = {**REGISTRY, "upstream": "10.43.0.1:8080"}
        data = _data([registry], ["10.43.0.1:8080/app:1"])
        connector = SkopeoConnector()
        with (
            patch.object(connector, "is_skopeo_available", True),
            patch("opi.services.catalog.image_registries.enforcers._connector", return_value=connector),
            patch("asyncio.create_subprocess_exec") as mock_exec,
            pytest.raises(FieldError) as exc,
        ):
            await RegistryTokenEnforcer().enforce(data, {"project_name": "demo"})
        mock_exec.assert_not_called()
        assert exc.value.field_path == "services/image-registries/config/registries[0]/upstream"
        assert str(exc.value) == "Het platform mag deze registry niet benaderen"

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

    async def test_zonder_skopeo_wordt_er_niets_geweigerd(self) -> None:
        """Geen skopeo is geen oordeel over het token, en die beslissing zit in de
        CONNECTOR zelf, daarom staat er in de enforcer geen tweede vangnet omheen.
        Gemeten op de echte methode met een connector die niet beschikbaar is."""
        from types import SimpleNamespace

        from opi.connectors.skopeo import SkopeoConnector

        niet_beschikbaar = SimpleNamespace(is_skopeo_available=False)
        ok, reason = await SkopeoConnector.check_repository_access(
            niet_beschikbaar,  # type: ignore[arg-type]
            "code.overheid.nl/robbert.uittenbroek/zad-deployment-demo",
            "robbert.uittenbroek",
            "een-token",
        )
        assert (ok, reason) == (True, "")

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

    async def test_een_upstream_zonder_pad_wordt_ook_getoetst(self) -> None:
        """Dezelfde invoer, alleen de upstream verschilt: ``ghcr.io`` tegenover
        ``ghcr.io/rijksictgilde``. Op de kale host sloeg de toets zwijgend over, want skopeo
        werd 0x aangeroepen, D5 werd niet geleverd, en het log meldde ten onrechte dat er
        nog geen image was."""
        kaal = {**REGISTRY, "upstream": "ghcr.io"}
        image = "ghcr.io/rijksictgilde/algoritmeregister/backend:2024.11.24"
        connector = _connector(False, "unauthorized: reqPackageAccess")
        data = _data([kaal], [image])
        with (
            patch("opi.services.catalog.image_registries.enforcers._connector", return_value=connector),
            pytest.raises(FieldError) as exc,
        ):
            await RegistryTokenEnforcer().enforce(data, {"project_name": "demo"})
        assert exc.value.field_path.endswith("registries[0]/password")
        connector.check_repository_access.assert_awaited_once_with(
            "ghcr.io/rijksictgilde/algoritmeregister/backend", "robbert.uittenbroek", "een-token"
        )


class TestDeConnector:
    """De maskering, want een foutregel van skopeo kan de verwijzing mét token bevatten."""

    def test_een_userinfo_in_een_foutmelding_wordt_gemaskeerd(self) -> None:
        from opi.connectors.skopeo import SkopeoConnector

        masked = SkopeoConnector._mask_userinfo("error pinging docker://robbert:geheim@code.overheid.nl/v2/")
        assert "geheim" not in masked
        assert "***@code.overheid.nl" in masked


class TestDeTokenConverter:
    """Wat het formulier leest en terugschrijft. De valkuil is de leesKANT: een leeg veld
    dat de gebruiker niet aanraakt komt als lege waarde terug, en die leest de schrijfkant
    als 'gewist'."""

    AGE = "-----BEGIN AGE ENCRYPTED FILE-----\nxxx\n-----END AGE ENCRYPTED FILE-----"

    def _converter(self) -> Any:
        from opi.services.catalog.image_registries.converters import ProjectAgeSecretConverter

        return ProjectAgeSecretConverter()

    def test_zonder_sleutel_blijft_het_blok_staan(self) -> None:
        """Anders toont het formulier een leeg veld, slaat de gebruiker op zonder iets aan
        te raken, en is het token weg."""
        gelezen = self._converter().read(self.AGE, context_data={})
        assert gelezen == self.AGE
        # En de schrijfkant laat hem dan met rust, want hij ziet de AGE-markering.
        assert self._converter().write(gelezen, context_data={}) == self.AGE

    def test_een_leesbare_waarde_komt_er_gewoon_uit(self) -> None:
        assert self._converter().read("nog-niet-versleuteld", context_data={}) == "nog-niet-versleuteld"

    def test_niets_ingevuld_blijft_niets(self) -> None:
        assert self._converter().read(None, context_data={}) == ""
        assert self._converter().read("", context_data={}) == ""

    def test_het_zicht_toont_nooit_de_waarde(self) -> None:
        assert self._converter().view(self.AGE) == "Versleuteld opgeslagen"
        assert self._converter().view("") == "Niet ingevuld"

    def test_zonder_publieke_sleutel_blijft_de_waarde_leesbaar_maar_bewaard(self) -> None:
        """Een token weggooien omdat we hem niet kunnen versleutelen is erger dan hem
        opslaan zoals hij is; de opslag zelf is SOPS-versleuteld."""
        assert self._converter().write("geheim", context_data={}) == "geheim"


def _keypair() -> tuple[str, str]:
    """Een echt AGE-sleutelpaar; de tests die het gebruiken slaan over zonder de binary."""
    result = subprocess.run(["age-keygen"], capture_output=True, text=True, check=True)
    lines = result.stdout.splitlines() + result.stderr.splitlines()
    private_key = next(line for line in lines if line.startswith("AGE-SECRET-KEY"))
    public_key = next(line.split(": ", 1)[1].strip() for line in lines if "public key:" in line.lower())
    return public_key, private_key


@pytest.mark.skipif(
    shutil.which("age") is None or shutil.which("age-keygen") is None,
    reason="age/age-keygen binary not available",
)
class TestDeConverterKentAlleDrieDeOpslagvormen:
    """Openen en ONGEWIJZIGD opslaan, per opslagvorm die het veld mag dragen.

    Dit is de gevaarlijkste kant van dezelfde vraag: toetst ``read()`` alleen op het
    armored blok, dan toont het formulier de cijfertekst van de eenregelige vorm, ziet
    ``write()`` daar geen AGE-markering in en versleutelt hem als NIEUW token, waarna het
    echte token weg is zonder dat iemand het veld heeft aangeraakt.
    """

    TOKEN = "ghp_HET_ECHTE_TOKEN"

    @pytest.fixture
    def project(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        system_public, system_private = _keypair()
        project_public, project_private = _keypair()
        monkeypatch.setattr("opi.core.config.settings.SOPS_AGE_PRIVATE_KEY", system_private)
        return {
            "config": {
                "age-public-key": project_public,
                "age-private-key": encrypt_age_content_sync(project_private, system_public),
            }
        }

    def _converter(self) -> Any:
        from opi.services.catalog.image_registries.converters import ProjectAgeSecretConverter

        return ProjectAgeSecretConverter()

    def _opgeslagen(self, project: dict[str, Any], vorm: str) -> str:
        armored = encrypt_age_content_sync(self.TOKEN, project["config"]["age-public-key"])
        return {
            "armored": armored,
            "base64+age": "base64+age:" + base64.b64encode(armored.encode()).decode(),
            "plain": f"plain:{self.TOKEN}",
        }[vorm]

    @pytest.mark.parametrize("vorm", ["armored", "base64+age", "plain"])
    def test_het_veld_toont_het_token_en_nooit_de_cijfertekst(self, project: dict[str, Any], vorm: str) -> None:
        assert self._converter().read(self._opgeslagen(project, vorm), context_data=project) == self.TOKEN

    @pytest.mark.parametrize("vorm", ["armored", "base64+age", "plain"])
    def test_ongewijzigd_opslaan_houdt_het_token(self, project: dict[str, Any], vorm: str) -> None:
        converter = self._converter()
        opgeslagen = self._opgeslagen(project, vorm)

        getoond = converter.read(opgeslagen, context_data=project)
        opnieuw = converter.write(getoond, context_data=project)

        assert converter.read(opnieuw, context_data=project) == self.TOKEN

    def test_een_eenregelige_cijfertekst_wordt_niet_opnieuw_versleuteld(self, project: dict[str, Any]) -> None:
        """De schrijfkant moet BEIDE versleutelde vormen herkennen, niet alleen het blok:
        anders komt de cijfertekst als nieuw token in een tweede laag AGE terecht."""
        opgeslagen = self._opgeslagen(project, "base64+age")

        assert self._converter().write(opgeslagen, context_data=project) == opgeslagen


@pytest.mark.asyncio
@pytest.mark.skipif(
    shutil.which("age") is None or shutil.which("age-keygen") is None,
    reason="age/age-keygen binary not available",
)
class TestDeToetsLangsDeOpslagroute:
    """De toets zoals hij in het echt draait: NA ``process_json_submission``.

    De modaal voor deze dienst heeft ``show_review=False`` en één sectie, dus de enige
    route erdoorheen is de stap-vooruit (``router_wizard.py`` / ``router_detail_edit.py``):
    verwerk de inzending, en toets daarna de UITKOMST daarvan. Die uitkomst draagt het
    token niet meer plat maar als armored AGE-blok, want ``ProjectAgeSecretConverter.write()``
    heeft het versleuteld. Een toets die de opgeslagen waarde onbewerkt aan skopeo geeft
    meet dus een token dat niemand heeft: een GELDIG token wordt geweigerd.

    Een aanroep van ``enforce()`` met een plat token in de dict komt daar niet achter,
    want die route bestaat in het formulier niet.
    """

    TOKEN = "ghp_HET_ECHTE_TOKEN"
    REEKS = "_services-config/image-registries/config/registries"

    @pytest.fixture
    def project(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        system_public, system_private = _keypair()
        project_public, project_private = _keypair()
        monkeypatch.setattr("opi.core.config.settings.SOPS_AGE_PRIVATE_KEY", system_private)
        return {
            "name": "demo",
            "services": [{"name": "image-registries", "config": {"registries": []}}],
            "components": [{"name": "web", "image": IMAGE}],
            "config": {
                "age-public-key": project_public,
                "age-private-key": encrypt_age_content_sync(project_private, system_public),
            },
        }

    def _section(self) -> Any:
        from opi.services.catalog.base import ConfigLayer
        from opi.services.registry import get_service
        from opi.services.services_enums import ServiceType

        section = get_service(ServiceType.IMAGE_REGISTRIES).config_form_section(ConfigLayer.PROJECT)
        assert section is not None
        assert section.enforcer is not None
        return section

    async def _opslaan(self, project: dict[str, Any], connector: Any) -> tuple[dict[str, Any], dict[str, list[str]]]:
        """De vorm van ``router_detail_edit.py:995-1026``: verwerken, dan de sectie toetsen."""
        import copy

        from opi.forms.editables.processor import EditableFormProcessor
        from opi.forms.editables.rendered_sequences import GERENDERDE_REEKSEN_VELD

        section = self._section()
        inzending = {
            "_services-config": {
                "image-registries": {
                    "config": {
                        "registries": [
                            {
                                "name": "code-overheid",
                                "upstream": "code.overheid.nl/robbert.uittenbroek",
                                "username": "robbert.uittenbroek",
                                # Wat de gebruiker intypt: het token zelf.
                                "password": self.TOKEN,
                            }
                        ]
                    }
                }
            },
            GERENDERDE_REEKSEN_VELD: [self.REEKS],
        }
        processor = EditableFormProcessor()
        submitted_yaml, errors = await processor.process_json_submission(
            inzending,
            section.editables,
            copy.deepcopy(project),
            edit_mode=True,
            enforcer_context={"project_name": "demo"},
        )
        assert not errors, errors
        with patch("opi.services.catalog.image_registries.enforcers._connector", return_value=connector):
            await processor.enforce_sections(
                submitted_yaml, [section], enforcer_context={"project_name": "demo"}, field_errors=errors
            )
        return submitted_yaml, errors

    async def test_de_verwerkte_inzending_draagt_het_token_versleuteld(self, project: dict[str, Any]) -> None:
        """De tegenproef op de opzet: zonder dit is de toets hieronder betekenisloos."""
        submitted_yaml, _ = await self._opslaan(project, _connector(True))

        opgeslagen = submitted_yaml["services"][0]["config"]["registries"][0]["password"]
        assert "BEGIN AGE ENCRYPTED FILE" in opgeslagen
        assert self.TOKEN not in opgeslagen

    async def test_een_geldig_token_gaat_langs_de_opslagroute_gewoon_door(self, project: dict[str, Any]) -> None:
        """De registry accepteert alleen het ECHTE token; het blok is een ander token."""
        connector = AsyncMock()
        connector.check_repository_access = AsyncMock(
            side_effect=lambda repository, username, password: (
                (True, "") if password == self.TOKEN else (False, "unauthorized: reqPackageAccess")
            )
        )

        _, errors = await self._opslaan(project, connector)

        assert errors == {}
        connector.check_repository_access.assert_awaited_once_with(
            "code.overheid.nl/robbert.uittenbroek/zad-deployment-demo", "robbert.uittenbroek", self.TOKEN
        )

    async def test_een_te_smal_token_komt_langs_dezelfde_route_wel_op_het_veld(self, project: dict[str, Any]) -> None:
        """De weigerende kant, zodat 'gaat door' niet gewoon 'toetst niets' is."""
        _, errors = await self._opslaan(project, _connector(False, "unauthorized: reqPackageAccess"))

        assert "services/image-registries/config/registries[0]/password" in errors
        assert "leesrecht op packages" in errors["services/image-registries/config/registries[0]/password"][0]
