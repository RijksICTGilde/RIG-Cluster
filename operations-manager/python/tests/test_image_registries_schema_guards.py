"""De twee schemabewakers die met ``registries[]`` mee hadden moeten verhuizen.

De sleutel is van de projectwortel naar de dienstconfig gegaan (v2.8 -> v2.9). Op de basis
droeg ``$defs/registry`` in ``project_v2.json`` twee constraints die daar niet louter
cosmetisch waren, en die zijn bij de verhuizing achtergebleven:

* ``url`` had het patroon ``^(?:(?:https?|ssh|git)://)?[^\\s\\u0000"]+\\Z``, dat een
  aanhalingsteken en een regeleinde juist verbood. Zonder dat patroon breekt een upstream
  uit zijn YAML-scalar in ``quay-proxy-organization.yaml.jinja`` en staan er extra
  DOCUMENTEN in het manifest -- willekeurige namespaced resources, aangemaakt door ArgoCD.
* ``password`` was een ``age-encrypted-or-plain``, en ``find_plaintext_secret_violations``
  herkent een AGE-veld AAN dat patroon. Zonder het patroon accepteerden de save-poorten een
  token in platte tekst, ook op de elf ``enforce_validation=False``-routes waar dat juist
  het enige is dat nog weigert.

Beide gaten worden hier gemeten waar ze bereikbaar waren: op de poorten die een save
draait, en op het gerenderde manifest.
"""

from __future__ import annotations

import os
from typing import Any

import pytest
import yaml
from opi.core.project_schema import ProjectIntegrityError, ProjectSchemaError, validate_project_schema
from opi.generation.manifests import ManifestGenerator
from opi.manager.project_validation import find_plaintext_service_config_violations, validate_service_configs
from opi.services.catalog.base import ProjectManifestContext
from opi.services.catalog.image_registries import ImageRegistriesService
from opi.services.catalog.image_registries.config_model import UPSTREAM_PATTERN
from pydantic import ValidationError

#: Een upstream die uit zijn scalar breekt en er twee documenten achteraan hangt, waarvan
#: het tweede een RoleBinding is naar de privileged SCC. Binnen de ``max_length=512`` van
#: ``AddRegistryByCredentialsRequest.url``, dus over de API bereikbaar.
YAML_INJECTIE_UPSTREAM = (
    'ghcr.io"\n'
    "---\n"
    "apiVersion: rbac.authorization.k8s.io/v1\n"
    "kind: RoleBinding\n"
    "metadata:\n"
    "  name: pwn\n"
    "  namespace: rig-prd-demo\n"
    "roleRef:\n"
    "  apiGroup: rbac.authorization.k8s.io\n"
    "  kind: ClusterRole\n"
    "  name: system:openshift:scc:privileged\n"
    "subjects:\n"
    "- kind: ServiceAccount\n"
    "  name: demo\n"
    "  namespace: rig-prd-demo\n"
    "---\n"
    "apiVersion: v1\n"
    "kind: ConfigMap\n"
    "spec:\n"
    "  proxyCache:\n"
    '    upstreamRegistry: "ghcr.io'
)

AGE_BLOCK = "-----BEGIN AGE ENCRYPTED FILE-----\nY2lwaGVydGV4dA==\n-----END AGE ENCRYPTED FILE-----"


def _project(registry: dict[str, Any]) -> dict[str, Any]:
    """Een projectbestand dat geldig BEGINT, met precies een registry in de dienstconfig."""
    return {
        "schema-version": 2.9,
        "name": "demo",
        "description": "Basisproject voor de schemabewakers",
        "users": [{"email": "iemand@rijksoverheid.nl", "role": "admin"}],
        "clusters": ["odcn-production"],
        "services": [{"name": "image-registries", "config": {"registries": [registry]}}],
        "config": {"age-public-key": "age1d489e9c48pmwam6603vecp7y29zz9fx5cgpe9uk6cu9l7asfzg9sx5s0tq"},
    }


def poorten(project_data: dict[str, Any]) -> None:
    """De twee synchrone poorten die ``ProjectStore._validate`` ook draait."""
    validate_project_schema(project_data)
    validate_service_configs(project_data)


class TestDeBasisIsZelfGeldig:
    """Zonder deze test meet elke andere test hier de basis in plaats van de bewaker."""

    def test_een_gewone_registry_komt_door_de_poorten(self) -> None:
        poorten(_project({"name": "eigen", "upstream": "ghcr.io", "username": "u", "password": AGE_BLOCK}))

    def test_de_twee_upstreams_die_de_vloot_echt_heeft(self) -> None:
        """Een verhuisde constraint mag geen bestaand bestand onopslaanbaar maken.

        ``ghcr.io`` (algor-odc) en ``rcr.rijksapps.nl/rig`` (dp-bn7) zijn de enige twee
        waarden die in de vloot voorkomen; de migratie 2.8 -> 2.9 zet ze letterlijk over.
        """
        for upstream in ("ghcr.io", "rcr.rijksapps.nl/rig"):
            poorten(_project({"name": "eigen", "upstream": upstream}))


class TestDeUpstreamKanNietUitZijnScalarBreken:
    def test_de_injectiepayload_sneuvelt_op_de_poorten(self) -> None:
        """Dit is de meting van de bevinding: op de branch zonder patroon kwam deze
        payload door alle drie de validaties heen."""
        with pytest.raises((ProjectSchemaError, ProjectIntegrityError)) as excinfo:
            poorten(_project({"name": "eigen", "upstream": YAML_INJECTIE_UPSTREAM}))
        assert "image-registries" in str(excinfo.value)

    @pytest.mark.parametrize(
        "upstream",
        [
            'ghcr.io"',  # het aanhalingsteken alleen al
            "ghcr.io\n",  # een afsluitende newline: de val van een ``$``-anker in Python
            "ghcr.io\nx: y",
            "https://ghcr.io",  # een protocol hoort niet in een image-verwijzing
            "GHCR.IO",  # de registry is hoofdlettergevoelig, dus kleine letters
            "ghcr.io/app:1.0",  # een tag hoort bij de image, niet bij de registry
            "ghcr.io/app@sha256:abc",
            "ghcr",  # geen host
        ],
    )
    def test_geweigerde_vormen(self, upstream: str) -> None:
        with pytest.raises((ProjectSchemaError, ProjectIntegrityError)):
            poorten(_project({"name": "eigen", "upstream": upstream}))

    @pytest.mark.parametrize(
        "upstream",
        [
            "ghcr.io",
            "code.overheid.nl/robbert.uittenbroek",
            "rcr.rijksapps.nl/rig",
            "localhost:5000",
            "registry.local:5000/team",
        ],
    )
    def test_toegestane_vormen(self, upstream: str) -> None:
        poorten(_project({"name": "eigen", "upstream": upstream}))

    def test_het_formulier_draait_dezelfde_regel_als_de_api(self) -> None:
        """Geen validator die alleen het formulier draait: de editable WIJST naar het model.

        Zou iemand de regel in het formulier opnieuw opschrijven, dan lopen de twee uit
        elkaar en accepteert de API wat het formulier weigert -- precies hoe deze payload
        langs de UpstreamValidator kwam.
        """
        from opi.services.catalog.image_registries.editables import REGISTRY_UPSTREAM_EDITABLE

        validator = REGISTRY_UPSTREAM_EDITABLE.validator
        assert validator is not None
        assert validator.validate("code.overheid.nl/robbert.uittenbroek") == []
        assert validator.validate(YAML_INJECTIE_UPSTREAM) != []
        assert validator.validate("https://ghcr.io") != []

    def test_de_api_weigert_de_payload_aan_de_deur(self) -> None:
        """``POST /projects/{p}/registries/by-credentials`` was de gemeten ingang."""
        import pydantic
        from opi.api.router import AddRegistryByCredentialsRequest

        AddRegistryByCredentialsRequest(name="eigen", url="ghcr.io", username="u", password="t")
        with pytest.raises(pydantic.ValidationError):
            AddRegistryByCredentialsRequest(name="eigen", url=YAML_INJECTIE_UPSTREAM, username="u", password="t")

    def test_het_gerenderde_manifest_blijft_een_document(self, tmp_path: Any) -> None:
        """De tweede grendel: ook als de waarde er langs zou komen, quote het sjabloon hem.

        Twee onafhankelijke sloten, want deze waarde reist ook binnendoor (een bestaand
        projectbestand, een migratie) en niet alleen door de poort die hem nu weigert.
        """
        spec = ImageRegistriesService().contribute_project_manifests(
            ProjectManifestContext(
                project_name="demo",
                project_data={
                    "name": "demo",
                    "services": [
                        {
                            "name": "image-registries",
                            "config": {"registries": [{"name": "eigen", "upstream": YAML_INJECTIE_UPSTREAM}]},
                        }
                    ],
                },
                cluster="odcn-production",
                namespace="rig-prd-demo",
            )
        )[-1]
        template_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "manifests")
        path = ManifestGenerator().create_manifest_file(
            template_path=os.path.join(template_dir, spec.template_path),
            values=spec.values,
            output_dir=str(tmp_path),
            output_filename=spec.filename,
        )
        with open(path) as handle:
            documents = list(yaml.safe_load_all(handle))
        assert len(documents) == 1, "de upstream heeft extra YAML-documenten in het manifest gezet"
        assert documents[0]["kind"] == "Organization"
        assert documents[0]["spec"]["proxyCache"]["upstreamRegistry"] == YAML_INJECTIE_UPSTREAM


class TestHetTokenValtNietUitDeFailClosedControle:
    def test_platte_tekst_wordt_geweigerd_op_de_poorten(self) -> None:
        with pytest.raises((ProjectSchemaError, ProjectIntegrityError)):
            poorten(_project({"name": "eigen", "upstream": "ghcr.io", "username": "u", "password": "ghp_KLARTEKST"}))

    def test_platte_tekst_wordt_ook_geweigerd_zonder_enforce(self) -> None:
        """De regel die er werkelijk toe doet: dit is wat op de elf
        ``enforce_validation=False``-routes nog overeind staat als de rest is doorgelaten.

        ``get_decrypted()`` ontsleutelt via ``decrypt_tree()`` generiek elke AGE-waarde in
        de boom, dus ook deze; een teruggeschreven view zou het token leesbaar in git
        zetten.
        """
        project = _project({"name": "eigen", "upstream": "ghcr.io", "username": "u", "password": "ghp_KLARTEKST"})
        assert find_plaintext_service_config_violations(project) == [
            "services/image-registries/config/registries/0/password"
        ]

    async def test_de_store_weigert_het_token_ook_zonder_enforce(self) -> None:
        """De AANHAKING, niet de functie: ``ProjectStore._validate`` roept beide helften aan.

        Zonder deze test blijft de volledige suite groen als iemand
        ``find_plaintext_service_config_violations`` uit die ene regel haalt, terwijl dat
        precies de weigering is die op de elf ``enforce_validation=False``-routes nog
        overeind staat. De helft ernaast heeft die test wel
        (``test_store_refuses_plaintext_secret_even_without_enforcement``); deze is zijn
        spiegelbeeld voor de dienstconfig.
        """
        from opi.services.project_store import GitProjectStore

        store = GitProjectStore(working_dir="/tmp/unused-by-this-test")
        leaked = _project({"name": "eigen", "upstream": "ghcr.io", "username": "u", "password": "ghp_KLARTEKST"})

        with pytest.raises(ProjectSchemaError, match="AGE-versleuteld"):
            await store._validate(leaked, enforce=False)

    def test_een_age_blok_is_geen_overtreding(self) -> None:
        project = _project({"name": "eigen", "upstream": "ghcr.io", "username": "u", "password": AGE_BLOCK})
        assert find_plaintext_service_config_violations(project) == []

    def test_een_leeg_token_is_geen_overtreding(self) -> None:
        """Een registry zonder inlog is een geldige invoer (een publieke upstream)."""
        assert find_plaintext_service_config_violations(_project({"name": "eigen", "upstream": "ghcr.io"})) == []

    def test_de_controle_leest_het_patroon_uit_het_model(self) -> None:
        """Afgeleid van het schema, niet van een handgeschreven veldenlijst.

        Haal het AGE-patroon van ``password`` weg en deze controle vindt niets meer -- dat
        is precies wat er bij de verhuizing gebeurde.
        """
        from opi.services.catalog.image_registries.config_model import RegistryEntry

        pattern = str(RegistryEntry.model_json_schema()["properties"]["password"]["anyOf"][0]["pattern"])
        assert "BEGIN AGE ENCRYPTED FILE" in pattern


class TestHetPatroonStaatOpEenPlek:
    def test_de_gecommitte_fragment_draagt_hetzelfde_patroon(self) -> None:
        """Het schemafragment wordt uit het model gerenderd; drift is uitgesloten."""
        import json
        from pathlib import Path

        fragment = json.loads(
            (
                Path(__file__).resolve().parent.parent
                / "opi/services/catalog/image_registries/image-registries.v1.0.json"
            ).read_text()
        )
        assert fragment["$defs"]["RegistryEntry"]["properties"]["upstream"]["pattern"] == UPSTREAM_PATTERN


class TestDeRegistrynaamHeeftDezelfdeRegelAlsHetFormulier:
    """De derde tweeling uit de securityreview: het formulier zette een
    ``KubernetesNameValidator`` op ``name`` en het model droeg er geen patroon, dus de API
    liet namen door die het formulier weigert. De regel staat nu in het model en het
    formulier wijst ernaar, net als bij ``upstream``.
    """

    @pytest.mark.parametrize(
        "name",
        [
            "Hoofdletters",
            "met spatie",
            "1-begint-met-cijfer",
            "eindigt-op-streepje-",
            "punt.in.de.naam",
            "onder_streep",
            "a" * 64,
        ],
    )
    def test_geweigerde_namen(self, name: str) -> None:
        with pytest.raises((ProjectSchemaError, ProjectIntegrityError)):
            poorten(_project({"name": name, "upstream": "ghcr.io"}))

    @pytest.mark.parametrize("name", ["code-overheid", "ghcr", "a", "registry-2"])
    def test_toegestane_namen(self, name: str) -> None:
        poorten(_project({"name": name, "upstream": "ghcr.io"}))

    def test_het_api_model_draagt_dezelfde_regel(self) -> None:
        """Het endpoint schrijft rechtstreeks tegen dit model; zonder het patroon daar komt
        een naam die het formulier weigert alsnog het projectbestand in."""
        from opi.api.router import AddRegistryByCredentialsRequest, AddRegistryBySecretRequest

        for model in (AddRegistryBySecretRequest, AddRegistryByCredentialsRequest):
            velden = {"name": "Hoofdletters", "url": "ghcr.io", "secretName": "s", "username": "u", "password": "p"}
            with pytest.raises(ValidationError):
                model(**velden)

    def test_het_formulier_en_het_model_wijzen_naar_dezelfde_regel(self) -> None:
        from opi.services.catalog.image_registries.editables import REGISTRY_NAME_EDITABLE

        assert REGISTRY_NAME_EDITABLE.validator is not None
        assert REGISTRY_NAME_EDITABLE.validator.validate("Hoofdletters")
        assert REGISTRY_NAME_EDITABLE.validator.validate("code-overheid") == []
