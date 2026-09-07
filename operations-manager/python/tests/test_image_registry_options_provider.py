"""De keuzelijst bij een component, en het vooruit invullen.

De afnemer vinkt de dienst aan bij een component en kiest dan welke registry. Past de
image-prefix bij PRECIES EEN registry, dan zetten we die vooruit, zodat hij in het gewone
geval alleen bevestigt. Passen er twee -- dezelfde upstream met verschillende tokens -- dan
IS de keuze het punt en zetten we niets voorop.
"""

from __future__ import annotations

from typing import Any

from opi.forms.visualizers.providers import ImageRegistryOptionsProvider

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


class TestDeLijstZelf:
    def test_de_registries_van_dit_project(self) -> None:
        assert _values(ImageRegistryOptionsProvider(yaml_data=_yaml([CODE, GHCR]))) == ["code-overheid", "ghcr"]

    def test_zonder_registries_een_uitleg_in_plaats_van_een_leeg_hokje(self) -> None:
        options = ImageRegistryOptionsProvider(yaml_data=_yaml([])).get_options()
        assert options[0]["value"] == ""
        assert "vul ze eerst in" in options[0]["label"]

    def test_een_opgeslagen_waarde_die_niet_meer_bestaat_blijft_kiesbaar(self) -> None:
        """Anders valt de volgende opslag terug op de eerste optie en verandert de
        configuratie zonder dat iemand er iets aan doet."""
        options = ImageRegistryOptionsProvider(yaml_data=_yaml([CODE]), current_value="weg").get_options()
        assert options[-1] == {"value": "weg", "label": "weg (bestaat niet meer)"}

    def test_hij_leest_ook_de_virtuele_wizardroot(self) -> None:
        """In de wizard staat de config onder ``_services-config`` en niet onder
        ``services``; een provider die alleen het echte pad vraagt geeft daar niets terug."""
        wizard = {
            "name": "demo",
            "services": ["image-registries"],
            "_services-config": [{"name": "image-registries", "config": {"registries": [CODE]}}],
        }
        assert _values(ImageRegistryOptionsProvider(yaml_data=wizard)) == ["code-overheid"]


class TestVooruitInvullen:
    def test_de_passende_registry_staat_voorop(self) -> None:
        provider = ImageRegistryOptionsProvider(
            yaml_data=_yaml([GHCR, CODE]), row_data={"image": "code.overheid.nl/team/app:1"}
        )
        assert _values(provider)[0] == "code-overheid"

    def test_zonder_image_verandert_de_volgorde_niet(self) -> None:
        assert _values(ImageRegistryOptionsProvider(yaml_data=_yaml([GHCR, CODE])))[0] == "ghcr"

    def test_bij_twee_passende_registries_zetten_we_niets_voorop(self) -> None:
        """Twee registries met dezelfde URL en verschillende tokens: dat is precies het
        geval waarvoor de koppeling een KEUZE is en geen afleiding."""
        provider = ImageRegistryOptionsProvider(
            yaml_data=_yaml([GHCR, CODE, TWEEDE_TEAM]), row_data={"image": "code.overheid.nl/team/app:1"}
        )
        assert _values(provider)[0] == "ghcr"

    def test_een_image_die_bij_geen_enkele_past_verandert_niets(self) -> None:
        provider = ImageRegistryOptionsProvider(yaml_data=_yaml([GHCR, CODE]), row_data={"image": "quay.io/x/y:1"})
        assert _values(provider)[0] == "ghcr"

    def test_de_prefix_matcht_op_segmentgrens(self) -> None:
        provider = ImageRegistryOptionsProvider(
            yaml_data=_yaml([GHCR, CODE]), row_data={"image": "code.overheid.nl/teamx/app:1"}
        )
        assert _values(provider)[0] == "ghcr"

    def test_zonder_row_data_leest_hij_de_index_uit_het_pad(self) -> None:
        provider = ImageRegistryOptionsProvider(
            yaml_data=_yaml(
                [GHCR, CODE], components=[{"name": "a"}, {"name": "b", "image": "code.overheid.nl/team/x:1"}]
            ),
            yaml_path="components[1]/services{image-registries}/config/registry",
        )
        assert _values(provider)[0] == "code-overheid"

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
        assert _values(provider) == ["github-registry", "anders"]

    def test_een_korte_naam_wordt_genormaliseerd(self) -> None:
        """``nginx:alpine`` is ``docker.io/library/nginx``; een registry op docker.io hoort
        hem dus te vangen."""
        docker = {"name": "hub", "upstream": "docker.io/library"}
        provider = ImageRegistryOptionsProvider(yaml_data=_yaml([GHCR, docker]), row_data={"image": "nginx:alpine"})
        assert _values(provider)[0] == "hub"
