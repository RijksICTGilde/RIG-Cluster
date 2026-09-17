"""De invoerhulp: wij fixen het onder water (RC-187).

Twee dingen die dezelfde belofte dragen: een geplakte browser-URL wordt een upstream, en
de naam mag een vrij label zijn waar wij de verwijzing uit afleiden.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from opi.forms.editables.editable import WidgetType
from opi.forms.editables.processor import EditableFormProcessor
from opi.forms.visualizers.providers import ImageRegistryOptionsProvider
from opi.services.catalog.base import ConfigLayer
from opi.services.catalog.image_registries.config_model import REGISTRY_NAME_PATTERN, RegistryEntry
from opi.services.catalog.image_registries.editables import REGISTRY_DISPLAY_NAME_EDITABLE, REGISTRY_UPSTREAM_EDITABLE
from opi.services.catalog.image_registries.naming import registry_slug
from opi.services.catalog.image_registries.upstream import normalize_upstream, upstream_from_project_images
from opi.services.catalog.image_registries.visualizers import REGISTRIES_SEQUENCE, REGISTRY_UPSTREAM
from opi.services.registry import get_service
from opi.services.schema_migration import relocate_registries_to_service
from opi.services.services_enums import ServiceType
from pydantic import ValidationError

#: Een entry draagt een gebruikersnaam plus token of een secretName (``RegistryEntry``); de
#: tests hier gaan over iets anders en geven daarom gewoon inloggegevens mee.
CREDS = {"username": "u", "password": "plain:een-token"}

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
        # De laatste twee zijn de combinatie die de losse gevallen missen: niet-letter
        # vooraan EN lang. Het ``r``-prefix schuift de afkapgrens dan een teken naar
        # links, en die grens kan midden in een streepje vallen.
        for label in ("Code Overheid", "2e registry", "A" * 90, "!!!", "9" + "a" * 57 + "-b", "9" + "a" * 90):
            slug = registry_slug(label, set())
            assert re.fullmatch(REGISTRY_NAME_PATTERN, slug), f"{label!r} -> {slug!r}"


def _project(registries: list[dict[str, Any]]) -> dict[str, Any]:
    return {"name": "demo", "services": [{"name": _SVC, "config": {"registries": registries}}]}


class TestDeSlugWordtAfgeleidOpBeideSchrijfwegen:
    def test_een_registry_zonder_naam_krijgt_er_een(self) -> None:
        project = _project([{"display-name": "Code Overheid", "upstream": "code.overheid.nl/team", **CREDS}])
        gegenereerd = get_service(ServiceType.IMAGE_REGISTRIES).generate_missing_values(project)
        assert project["services"][0]["config"]["registries"][0]["name"] == "code-overheid"
        assert gegenereerd == {"services/image-registries/config/registries[0]/name": "code-overheid"}

    def test_een_bestaande_naam_verandert_niet_mee_met_het_label(self) -> None:
        """De slug is de verwijzing vanaf componenten en zit in de naam van het
        pull-secret, dus hij ligt vast zodra hij bestaat."""
        project = _project(
            [{"name": "oude-naam", "display-name": "Een hele andere naam", "upstream": "ghcr.io", **CREDS}]
        )
        assert get_service(ServiceType.IMAGE_REGISTRIES).generate_missing_values(project) == {}
        assert project["services"][0]["config"]["registries"][0]["name"] == "oude-naam"

    def test_twee_labels_die_dezelfde_slug_geven_botsen_niet(self) -> None:
        project = _project(
            [
                {"display-name": "Code Overheid", "upstream": "code.overheid.nl/a", **CREDS},
                {"display-name": "code overheid", "upstream": "code.overheid.nl/b", **CREDS},
            ]
        )
        get_service(ServiceType.IMAGE_REGISTRIES).generate_missing_values(project)
        namen = [entry["name"] for entry in project["services"][0]["config"]["registries"]]
        assert namen == ["code-overheid", "code-overheid-2"]

    def test_een_nieuwe_slug_wijkt_uit_voor_een_bestaande(self) -> None:
        project = _project(
            [
                {"name": "code-overheid", "upstream": "code.overheid.nl/a", **CREDS},
                {"display-name": "Code Overheid", "upstream": "code.overheid.nl/b", **CREDS},
            ]
        )
        get_service(ServiceType.IMAGE_REGISTRIES).generate_missing_values(project)
        namen = [entry["name"] for entry in project["services"][0]["config"]["registries"]]
        assert namen == ["code-overheid", "code-overheid-2"]

    def test_de_portal_loopt_langs_dezelfde_functie(self) -> None:
        """Via ``post_merge`` van de configsectie, zodat de twee wegen geen registry kunnen
        opleveren die de ene wel een naam geeft en de andere niet."""
        service = get_service(ServiceType.IMAGE_REGISTRIES)
        section = service.config_form_section(ConfigLayer.PROJECT)
        assert section is not None
        assert section.post_merge is not None
        project = _project([{"display-name": "Code Overheid", "upstream": "code.overheid.nl/team", **CREDS}])
        section.post_merge(project, {})
        assert project["services"][0]["config"]["registries"][0]["name"] == "code-overheid"

    def test_een_entry_zonder_naam_en_zonder_label_wordt_geweigerd(self) -> None:
        """Anders is hij nergens naar te verwijzen en onzichtbaar voor de hele dienst."""
        with pytest.raises(ValidationError):
            RegistryEntry(upstream="ghcr.io", **CREDS)


class TestHetScherm:
    def test_de_keuzelijst_toont_het_label_en_verwijst_met_de_slug(self) -> None:
        project = _project([{"name": "code-overheid", "display-name": "Code Overheid", "upstream": "ghcr.io", **CREDS}])
        opties = ImageRegistryOptionsProvider(yaml_data=project).get_options()
        assert opties[1] == {"value": "code-overheid", "label": "Code Overheid"}

    def test_zonder_label_blijft_de_slug_op_het_scherm(self) -> None:
        project = _project([{"name": "code-overheid", "upstream": "ghcr.io", **CREDS}])
        opties = ImageRegistryOptionsProvider(yaml_data=project).get_options()
        assert opties[1] == {"value": "code-overheid", "label": "code-overheid"}

    def test_de_slug_komt_mee_in_de_inzending_maar_niet_op_het_scherm(self) -> None:
        """Zonder die sleutel koppelt ``_match_original_item`` de rijen op INDEX, en schuift
        bij het weghalen van een rij de slug van de ene registry onder de andere."""
        per_pad = {kind.editable.yaml_path.rsplit("/", 1)[-1]: kind for kind in REGISTRIES_SEQUENCE.children or []}
        assert per_pad["name"].widget is WidgetType.HIDDEN
        assert per_pad["display-name"].widget is WidgetType.TEXT


class TestEenBestaandeRegistryBlijftOpslaanbaar:
    """Een registry van voor RC-187 heeft alleen een slug en geen label.

    Zou het label verplicht zijn, dan sneuvelde de eerstvolgende opslag van zo'n project op
    een veld dat er nooit was.
    """

    def test_zonder_label_is_de_entry_geldig(self) -> None:
        assert RegistryEntry(name="code-overheid", upstream="ghcr.io", **CREDS).display_name is None

    def test_het_labelveld_is_niet_verplicht(self) -> None:
        assert REGISTRY_DISPLAY_NAME_EDITABLE.required is False

    def test_een_leeggemaakt_label_verdwijnt_uit_het_bestand(self) -> None:
        """Leeg laten betekent geen label, en dan hoort er ook geen lege sleutel te staan."""
        project = _project([{"name": "code-overheid", "display-name": "Code Overheid", "upstream": "ghcr.io", **CREDS}])
        EditableFormProcessor._write_field(
            REGISTRY_DISPLAY_NAME_EDITABLE, "services/image-registries/config/registries[0]/display-name", "", project
        )
        assert "display-name" not in project["services"][0]["config"]["registries"][0]

    def test_de_regel_gaat_over_de_twee_velden_samen(self) -> None:
        """Niet "label verplicht" maar "ergens naar te verwijzen", en die regel staat in het
        model zodat de API hem ook draagt."""
        RegistryEntry(name="code-overheid", upstream="ghcr.io", **CREDS)
        RegistryEntry(**{"display-name": "Code Overheid", "upstream": "ghcr.io", **CREDS})
        with pytest.raises(ValidationError, match="Geef een 'name' of een 'display-name'"):
            RegistryEntry(upstream="ghcr.io", **CREDS)


class TestDeUpstreamWordtVooruitIngevuldUitDeImage:
    """De afnemer heeft zijn image al ingetypt; de upstream staat daarin.

    Wij kunnen hem niet weglaten -- op ODCN wordt er een proxy-organisatie aangemaakt voor
    precies EEN upstream-namespace, en die moet er zijn voordat er een image is -- dus nemen
    we de vraag weg in plaats van het veld.
    """

    def test_uit_een_image_op_een_component(self) -> None:
        project = {"components": [{"name": "web", "image": "code.overheid.nl/team/app:1.2"}]}
        assert upstream_from_project_images(project) == "code.overheid.nl/team"

    def test_uit_een_image_op_een_deployment(self) -> None:
        project = {"deployments": [{"components": [{"reference": "web", "image": "ghcr.io/org/app:1"}]}]}
        assert upstream_from_project_images(project) == "ghcr.io/org"

    def test_twee_prefixen_leveren_niets_op(self) -> None:
        """Dan is de vraag juist het punt, en zou een gok een registry opleveren die
        nergens bij hoort."""
        project = {"components": [{"image": "code.overheid.nl/team/a:1"}, {"image": "ghcr.io/org/b:2"}]}
        assert upstream_from_project_images(project) is None

    def test_dezelfde_prefix_twee_keer_is_wel_eenduidig(self) -> None:
        project = {"components": [{"image": "code.overheid.nl/team/a:1"}, {"image": "code.overheid.nl/team/b:2"}]}
        assert upstream_from_project_images(project) == "code.overheid.nl/team"

    def test_een_image_zonder_host_telt_niet_mee(self) -> None:
        """``nginx:alpine`` staat op Docker Hub, en daar heb je geen eigen registry voor."""
        assert upstream_from_project_images({"components": [{"image": "nginx:alpine"}]}) is None

    def test_zonder_images_wordt_er_niets_ingevuld(self) -> None:
        assert upstream_from_project_images({}) is None

    def test_het_veld_draagt_die_default(self) -> None:
        assert REGISTRY_UPSTREAM_EDITABLE.default is upstream_from_project_images

    def test_een_bestaande_upstream_wordt_er_niet_door_overschreven(self) -> None:
        """De default geldt alleen voor een rij die er nog geen draagt, dus voor een nieuwe
        registry; anders zou het openen van het blok bestaande registries verbouwen."""
        processor = EditableFormProcessor()
        processor._yaml_data = {"components": [{"image": "code.overheid.nl/team/app:1"}]}
        assert processor._effective_value(REGISTRY_UPSTREAM, "ghcr.io/anders") == "ghcr.io/anders"
        assert processor._effective_value(REGISTRY_UPSTREAM, "") == "code.overheid.nl/team"
