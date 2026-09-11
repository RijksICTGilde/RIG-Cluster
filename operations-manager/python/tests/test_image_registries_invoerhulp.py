"""De invoerhulp: wij fixen het onder water (RC-187).

Twee dingen die dezelfde belofte dragen: een geplakte browser-URL wordt een upstream, en
de naam mag een vrij label zijn waar wij de verwijzing uit afleiden.
"""

from __future__ import annotations

from typing import Any

import pytest
from opi.services.catalog.image_registries.naming import registry_slug
from opi.services.catalog.image_registries.upstream import normalize_upstream
from opi.services.registry import get_service
from opi.services.services_enums import ServiceType

_SVC = ServiceType.IMAGE_REGISTRIES.value


class TestEenGeplakteUrlWordtEenUpstream:
    @pytest.mark.parametrize(
        ("geplakt", "verwacht"),
        [
            # Forgejo/Gitea: alles vanaf ``/-/`` is browserruis.
            ("https://code.overheid.nl/robbert.uittenbroek/-/packages", "code.overheid.nl/robbert.uittenbroek"),
            ("code.overheid.nl/robbert/-/packages/container/demo/0a611d9d", "code.overheid.nl/robbert"),
            # GitHub: de packages staan op ghcr.io en niet op github.com.
            ("https://github.com/orgs/rijksictgilde/packages", "ghcr.io/rijksictgilde"),
            ("https://github.com/users/robbert/packages?tab=packages", "ghcr.io/robbert"),
            ("https://github.com/rijksictgilde/algoritmeregister/pkgs/container/backend", "ghcr.io/rijksictgilde"),
            # Docker Hub.
            ("https://hub.docker.com/r/bitnami/nginx", "docker.io/bitnami"),
            ("https://hub.docker.com/u/bitnami", "docker.io/bitnami"),
            ("https://hub.docker.com/_/nginx", "docker.io/library"),
            # GitLab: de registry staat op een andere host dan de browser.
            ("https://gitlab.com/groep/project/container_registry", "registry.gitlab.com/groep/project"),
            ("https://gitlab.com/groep/project/-/container_registry/1234", "registry.gitlab.com/groep/project"),
            # Een volledige image-referentie: de tag valt weg en daarmee de repositorynaam.
            ("code.overheid.nl/team/app:1.2", "code.overheid.nl/team"),
            (
                "ghcr.io/rijksictgilde/algoritmeregister/backend@sha256:abc123",
                "ghcr.io/rijksictgilde/algoritmeregister",
            ),
            # De generieke bewerkingen.
            ("HTTPS://GHCR.IO/", "ghcr.io"),
            ("  ghcr.io  ", "ghcr.io"),
        ],
    )
    def test_de_bekende_vormen(self, geplakt: str, verwacht: str) -> None:
        assert normalize_upstream(geplakt) == verwacht

    @pytest.mark.parametrize(
        "upstream",
        ["ghcr.io", "code.overheid.nl/robbert.uittenbroek", "rcr.rijksapps.nl/rig", "localhost:5000"],
    )
    def test_een_upstream_die_al_goed_is_blijft_letterlijk(self, upstream: str) -> None:
        """Ook de twee vormen die de vloot echt heeft; een hulp die die zou verbouwen is
        geen hulp."""
        assert normalize_upstream(upstream) == upstream

    def test_een_pad_zonder_tag_raken_we_niet_aan(self) -> None:
        """Alleen een tag of digest onderscheidt een image-referentie van een upstream met
        een pad. Zonder dat onderscheid zou ``code.overheid.nl/team`` ``code.overheid.nl``
        worden en wees de registry naar iedereens packages."""
        assert normalize_upstream("code.overheid.nl/team") == "code.overheid.nl/team"

    def test_userinfo_blijft_staan(self) -> None:
        """Wegknippen zou een upstream opleveren die er geldig uitziet maar ergens anders
        heen wijst; het patroon weigert hem nu, en dat is de bedoeling."""
        assert normalize_upstream("ssh://git@host/x") == "git@host/x"

    def test_een_eigen_gitlab_wordt_niet_geraden(self) -> None:
        """Bij een eigen GitLab is de registryhost een installatiekeuze die wij niet kunnen
        weten; een gok zou een upstream opleveren die nergens bestaat."""
        geplakt = "https://gitlab.intern.nl/groep/project/container_registry"
        assert normalize_upstream(geplakt) == "gitlab.intern.nl/groep/project/container_registry"

    def test_de_migratie_draait_dezelfde_omzetting(self) -> None:
        """Een bestaand bestand hoort niet door een andere regel te gaan dan wat een
        afnemer vandaag intypt."""
        from opi.services.schema_migration import relocate_registries_to_service

        project: dict[str, Any] = {"registries": [{"name": "a", "url": "HTTPS://GHCR.IO/"}]}
        relocate_registries_to_service(project)
        assert project["services"][0]["config"]["registries"][0]["upstream"] == "ghcr.io"


class TestDeNaamMagEenVrijLabelZijn:
    def test_de_slug_volgt_uit_het_label(self) -> None:
        assert registry_slug("Code Overheid", set()) == "code-overheid"

    def test_een_label_dat_met_een_cijfer_begint_krijgt_een_letter(self) -> None:
        """``REGISTRY_NAME_PATTERN`` eist een kleine letter vooraan, zodat de naam nooit
        als YAML-getal wordt gelezen."""
        assert registry_slug("2e registry", set()) == "r2e-registry"

    def test_een_botsing_krijgt_een_volgnummer(self) -> None:
        assert registry_slug("Code Overheid", {"code-overheid"}) == "code-overheid-2"
        assert registry_slug("Code Overheid", {"code-overheid", "code-overheid-2"}) == "code-overheid-3"

    def test_de_slug_past_in_het_patroon(self) -> None:
        import re

        from opi.services.catalog.image_registries.config_model import REGISTRY_NAME_PATTERN

        for label in ("Code Overheid", "2e registry", "A" * 90, "!!!"):
            assert re.match(REGISTRY_NAME_PATTERN, registry_slug(label, set()))


def _project(registries: list[dict[str, Any]]) -> dict[str, Any]:
    return {"name": "demo", "services": [{"name": _SVC, "config": {"registries": registries}}]}


class TestDeSlugWordtAfgeleidOpBeideSchrijfwegen:
    def test_een_registry_zonder_naam_krijgt_er_een(self) -> None:
        project = _project([{"display-name": "Code Overheid", "upstream": "code.overheid.nl/team"}])
        gegenereerd = get_service(ServiceType.IMAGE_REGISTRIES).generate_missing_values(project)
        assert project["services"][0]["config"]["registries"][0]["name"] == "code-overheid"
        assert gegenereerd == {"services/image-registries/config/registries[0]/name": "code-overheid"}

    def test_een_bestaande_naam_verandert_niet_mee_met_het_label(self) -> None:
        """De slug is de verwijzing vanaf componenten en zit in de naam van het
        pull-secret, dus hij ligt vast zodra hij bestaat."""
        project = _project([{"name": "oude-naam", "display-name": "Een hele andere naam", "upstream": "ghcr.io"}])
        assert get_service(ServiceType.IMAGE_REGISTRIES).generate_missing_values(project) == {}
        assert project["services"][0]["config"]["registries"][0]["name"] == "oude-naam"

    def test_twee_labels_die_dezelfde_slug_geven_botsen_niet(self) -> None:
        project = _project(
            [
                {"display-name": "Code Overheid", "upstream": "code.overheid.nl/a"},
                {"display-name": "code overheid", "upstream": "code.overheid.nl/b"},
            ]
        )
        get_service(ServiceType.IMAGE_REGISTRIES).generate_missing_values(project)
        namen = [entry["name"] for entry in project["services"][0]["config"]["registries"]]
        assert namen == ["code-overheid", "code-overheid-2"]

    def test_een_nieuwe_slug_wijkt_uit_voor_een_bestaande(self) -> None:
        project = _project(
            [
                {"name": "code-overheid", "upstream": "code.overheid.nl/a"},
                {"display-name": "Code Overheid", "upstream": "code.overheid.nl/b"},
            ]
        )
        get_service(ServiceType.IMAGE_REGISTRIES).generate_missing_values(project)
        namen = [entry["name"] for entry in project["services"][0]["config"]["registries"]]
        assert namen == ["code-overheid", "code-overheid-2"]

    def test_de_portal_loopt_langs_dezelfde_functie(self) -> None:
        """Via ``post_merge`` van de configsectie, zodat de twee wegen geen registry kunnen
        opleveren die de ene wel een naam geeft en de andere niet."""
        from opi.services.catalog.base import ConfigLayer

        service = get_service(ServiceType.IMAGE_REGISTRIES)
        section = service.config_form_section(ConfigLayer.PROJECT)
        assert section is not None
        assert section.post_merge is not None
        project = _project([{"display-name": "Code Overheid", "upstream": "code.overheid.nl/team"}])
        section.post_merge(project, {})
        assert project["services"][0]["config"]["registries"][0]["name"] == "code-overheid"

    def test_een_entry_zonder_naam_en_zonder_label_wordt_geweigerd(self) -> None:
        """Anders is hij nergens naar te verwijzen en onzichtbaar voor de hele dienst."""
        from opi.services.catalog.image_registries.config_model import RegistryEntry
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            RegistryEntry(upstream="ghcr.io")


class TestHetScherm:
    def test_de_keuzelijst_toont_het_label_en_verwijst_met_de_slug(self) -> None:
        from opi.forms.visualizers.providers import ImageRegistryOptionsProvider

        project = _project([{"name": "code-overheid", "display-name": "Code Overheid", "upstream": "ghcr.io"}])
        opties = ImageRegistryOptionsProvider(yaml_data=project).get_options()
        assert opties[1] == {"value": "code-overheid", "label": "Code Overheid"}

    def test_zonder_label_blijft_de_slug_op_het_scherm(self) -> None:
        from opi.forms.visualizers.providers import ImageRegistryOptionsProvider

        project = _project([{"name": "code-overheid", "upstream": "ghcr.io"}])
        opties = ImageRegistryOptionsProvider(yaml_data=project).get_options()
        assert opties[1] == {"value": "code-overheid", "label": "code-overheid"}

    def test_de_slug_komt_mee_in_de_inzending_maar_niet_op_het_scherm(self) -> None:
        """Zonder die sleutel koppelt ``_match_original_item`` de rijen op INDEX, en schuift
        bij het weghalen van een rij de slug van de ene registry onder de andere."""
        from opi.forms.editables.editable import WidgetType
        from opi.services.catalog.image_registries.visualizers import REGISTRIES_SEQUENCE

        per_pad = {kind.editable.yaml_path.rsplit("/", 1)[-1]: kind for kind in REGISTRIES_SEQUENCE.children or []}
        assert per_pad["name"].widget is WidgetType.HIDDEN
        assert per_pad["display-name"].widget is WidgetType.TEXT


class TestEenBestaandeRegistryBlijftOpslaanbaar:
    """Een registry van voor RC-187 heeft alleen een slug en geen label.

    Zou het label verplicht zijn, dan sneuvelde de eerstvolgende opslag van zo'n project op
    een veld dat er nooit was.
    """

    def test_zonder_label_is_de_entry_geldig(self) -> None:
        from opi.services.catalog.image_registries.config_model import RegistryEntry

        assert RegistryEntry(name="code-overheid", upstream="ghcr.io").display_name is None

    def test_het_labelveld_is_niet_verplicht(self) -> None:
        from opi.services.catalog.image_registries.editables import REGISTRY_DISPLAY_NAME_EDITABLE

        assert REGISTRY_DISPLAY_NAME_EDITABLE.required is False

    def test_de_regel_gaat_over_de_twee_velden_samen(self) -> None:
        """Niet "label verplicht" maar "ergens naar te verwijzen", en die regel staat in het
        model zodat de API hem ook draagt."""
        from opi.services.catalog.image_registries.config_model import RegistryEntry
        from pydantic import ValidationError

        RegistryEntry(name="code-overheid", upstream="ghcr.io")
        RegistryEntry(**{"display-name": "Code Overheid", "upstream": "ghcr.io"})
        with pytest.raises(ValidationError):
            RegistryEntry(upstream="ghcr.io")
