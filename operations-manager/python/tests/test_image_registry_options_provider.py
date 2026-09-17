"""De keuzelijst bij een component, en het vooruit invullen.

De afnemer kiest bij het image-veld waar die image vandaan komt; de keuze IS de selectie
van de dienst op dat component (RC-187). Vandaar de standaardwaarde vooraan: automatisch,
en niets in het bestand. Past de image-prefix bij PRECIES EEN registry, dan
zetten we die vooruit, zodat hij in het gewone geval alleen bevestigt. Passen er twee
(dezelfde upstream met verschillende tokens), dan IS de keuze het punt en zetten we niets
voorop.

De lijst is ook de ZICHTBAARHEID: geen registries betekent geen opties, en dus geen veld.
"""

from __future__ import annotations

from typing import Any

from opi.forms.visualizers.providers import (
    AUTOMATIC_REGISTRY_LABEL,
    INHERIT_REGISTRY_LABEL,
    ImageRegistryOptionsProvider,
)

CODE = {"name": "code-overheid", "upstream": "code.overheid.nl/team"}
GHCR = {"name": "ghcr", "upstream": "ghcr.io/org"}
TWEEDE_TEAM = {"name": "team-tweede", "upstream": "code.overheid.nl/team"}


def _yaml(registries: list[dict[str, Any]], components: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "name": "demo",
        "services": [{"name": "image-registries", "config": {"registries": registries}}],
        "components": components or [],
    }


def _values(provider: ImageRegistryOptionsProvider) -> list[str]:
    return [option["value"] for option in provider.get_options()]


def _registries(provider: ImageRegistryOptionsProvider) -> list[str]:
    """De registries zelf, dus zonder de standaardwaarde die vooraan staat."""
    return [value for value in _values(provider) if value]


class TestDeLijstZelf:
    def test_de_registries_van_dit_project(self) -> None:
        assert _values(ImageRegistryOptionsProvider(yaml_data=_yaml([CODE, GHCR]))) == [
            "",
            "code-overheid",
            "ghcr",
        ]

    def test_de_standaardwaarde_staat_vooraan_en_schrijft_niets_weg(self) -> None:
        options = ImageRegistryOptionsProvider(yaml_data=_yaml([CODE])).get_options()
        assert options[0] == {"value": "", "label": AUTOMATIC_REGISTRY_LABEL}

    def test_het_label_belooft_geen_publieke_weg(self) -> None:
        """Zonder keuze gaat een image onder een eigen upstream toch langs die registry
        (``TestEenEigenRegistryGeldtVoorHetHeleProject``), dus het label moet dat zeggen."""
        assert "eigen registry" in AUTOMATIC_REGISTRY_LABEL
        assert "geen token" not in AUTOMATIC_REGISTRY_LABEL

    def test_bij_een_deployment_component_betekent_leeg_iets_anders(self) -> None:
        """Daar is de niet-waarde geen "automatisch" maar "geen afwijking van het component"."""
        options = ImageRegistryOptionsProvider(
            yaml_data=_yaml([CODE]),
            yaml_path="deployments[0]/components[0]/services/image-registries/config/registry",
        ).get_options()
        assert options[0] == {"value": "", "label": INHERIT_REGISTRY_LABEL}

    def test_zonder_registries_valt_er_niets_te_kiezen_en_is_er_geen_veld(self) -> None:
        """Een lege lijst is de zichtbaarheid: de componentvorm blijft zoals hij was, en
        dat is de toestand van 47 van de 49 projecten."""
        assert ImageRegistryOptionsProvider(yaml_data=_yaml([])).get_options() == []

    def test_een_opgeslagen_waarde_die_niet_meer_bestaat_blijft_kiesbaar(self) -> None:
        """Anders valt de volgende opslag terug op de eerste optie en verandert de
        configuratie zonder dat iemand er iets aan doet."""
        options = ImageRegistryOptionsProvider(yaml_data=_yaml([CODE]), current_value="weg").get_options()
        assert options[-1] == {"value": "weg", "label": "weg (bestaat niet meer)"}

    def test_een_verwijzing_zonder_registries_houdt_het_veld_op_het_scherm(self) -> None:
        """Zou het veld hier verdwijnen, dan zag niemand meer waar die image vandaan komt
        terwijl de verwijzing in het bestand blijft staan."""
        options = ImageRegistryOptionsProvider(yaml_data=_yaml([]), current_value="weg").get_options()
        assert [option["value"] for option in options] == ["", "weg"]

    def test_hij_leest_ook_de_virtuele_wizardroot(self) -> None:
        """In de wizard staat de config onder ``_services-config`` en niet onder
        ``services``; een provider die alleen het echte pad vraagt geeft daar niets terug."""
        wizard = {
            "name": "demo",
            "services": ["image-registries"],
            "_services-config": [{"name": "image-registries", "config": {"registries": [CODE]}}],
        }
        assert _registries(ImageRegistryOptionsProvider(yaml_data=wizard)) == ["code-overheid"]


class TestVooruitInvullen:
    def test_de_passende_registry_staat_voorop(self) -> None:
        provider = ImageRegistryOptionsProvider(
            yaml_data=_yaml([GHCR, CODE]), row_data={"image": "code.overheid.nl/team/app:1"}
        )
        assert _registries(provider)[0] == "code-overheid"

    def test_zonder_image_verandert_de_volgorde_niet(self) -> None:
        assert _registries(ImageRegistryOptionsProvider(yaml_data=_yaml([GHCR, CODE])))[0] == "ghcr"

    def test_bij_twee_passende_registries_zetten_we_niets_voorop(self) -> None:
        """Twee registries met dezelfde URL en verschillende tokens: dat is precies het
        geval waarvoor de koppeling een KEUZE is en geen afleiding."""
        provider = ImageRegistryOptionsProvider(
            yaml_data=_yaml([GHCR, CODE, TWEEDE_TEAM]), row_data={"image": "code.overheid.nl/team/app:1"}
        )
        assert _registries(provider)[0] == "ghcr"

    def test_een_image_die_bij_geen_enkele_past_verandert_niets(self) -> None:
        provider = ImageRegistryOptionsProvider(yaml_data=_yaml([GHCR, CODE]), row_data={"image": "quay.io/x/y:1"})
        assert _registries(provider)[0] == "ghcr"

    def test_de_prefix_matcht_op_segmentgrens(self) -> None:
        provider = ImageRegistryOptionsProvider(
            yaml_data=_yaml([GHCR, CODE]), row_data={"image": "code.overheid.nl/teamx/app:1"}
        )
        assert _registries(provider)[0] == "ghcr"

    def test_zonder_row_data_leest_hij_de_index_uit_het_pad(self) -> None:
        provider = ImageRegistryOptionsProvider(
            yaml_data=_yaml(
                [GHCR, CODE], components=[{"name": "a"}, {"name": "b", "image": "code.overheid.nl/team/x:1"}]
            ),
            yaml_path="components[1]/services{image-registries}/config/registry",
        )
        assert _registries(provider)[0] == "code-overheid"

    def test_een_upstream_zonder_pad_komt_ook_voorop(self) -> None:
        """De vorm die de vloot echt heeft (``algor-odc``: ``ghcr.io``). De upstream werd
        eerder door de IMAGE-normalisatie gehaald en werd dan ``docker.io/library/ghcr.io``,
        dus de passende registry bleef staan waar hij stond en de afnemer moest zelf zoeken.
        """
        kaal = {"name": "github-registry", "upstream": "ghcr.io"}
        anders = {"name": "anders", "upstream": "quay.io/team"}
        provider = ImageRegistryOptionsProvider(
            yaml_data=_yaml([anders, kaal]),
            row_data={"image": "ghcr.io/rijksictgilde/algoritmeregister/backend:2024.11.24"},
        )
        assert _registries(provider) == ["github-registry", "anders"]

    def test_een_korte_naam_wordt_genormaliseerd(self) -> None:
        """``nginx:alpine`` is ``docker.io/library/nginx``; een registry op docker.io hoort
        hem dus te vangen."""
        docker = {"name": "hub", "upstream": "docker.io/library"}
        provider = ImageRegistryOptionsProvider(yaml_data=_yaml([GHCR, docker]), row_data={"image": "nginx:alpine"})
        assert _registries(provider)[0] == "hub"
