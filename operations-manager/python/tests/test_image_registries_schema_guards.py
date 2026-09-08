"""De twee schemabewakers die met ``registries[]`` mee hadden moeten verhuizen.

De sleutel is van de projectwortel naar de dienstconfig gegaan (v2.8 -> v2.9). Op de basis
droeg ``$defs/registry`` in ``project_v2.json`` twee constraints die daar niet louter
cosmetisch waren, en die zijn bij de verhuizing achtergebleven:

* ``url`` had het patroon ``^(?:(?:https?|ssh|git)://)?[^\\s\\u0000"]+\\Z``, dat een
  aanhalingsteken en een regeleinde juist verbood. Zonder dat patroon breekt een upstream
  uit zijn YAML-scalar in ``quay-proxy-organization.yaml.jinja`` en staan er extra
  DOCUMENTEN in het manifest: willekeurige namespaced resources, aangemaakt door ArgoCD.
* ``password`` was een ``age-encrypted-or-plain``, en ``find_plaintext_secret_violations``
  herkent een AGE-veld AAN dat patroon. Zonder het patroon accepteerden de save-poorten een
  token in platte tekst, ook op de elf ``enforce_validation=False``-routes waar dat juist
  het enige is dat nog weigert.

Beide gaten worden hier gemeten waar ze bereikbaar waren: op de poorten die een save
draait, en op het gerenderde manifest.
"""

from __future__ import annotations

import os
import re
from typing import Any

import pytest
import yaml
from opi.core.project_schema import ProjectIntegrityError, ProjectSchemaError, validate_project_schema
from opi.generation.manifests import ManifestGenerator, render_template
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
        elkaar en accepteert de API wat het formulier weigert, precies hoe deze payload
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

        Haal het AGE-patroon van ``password`` weg en deze controle vindt niets meer, dat
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


#: De payload uit de securityreview op ``secretName``. Hij zet eerst twee podvelden bij de
#: pod van het component (``hostNetwork``, ``hostPID``) en hangt er dan een TWEEDE document
#: achteraan: een RoleBinding naar ClusterRole ``cluster-admin`` op de ``default``
#: serviceaccount van de eigen namespace. Het derde document heropent een mapping op inspring
#: 6, zodat de rest van de Deployment blijft parsen en het geheel een geldig manifest is dat
#: ArgoCD toepast (``namespaceResourceWhitelist`` is group '*', kind '*').
SECRET_NAME_INJECTIE = (
    "rig-robot-pull-secret\n"
    "      hostNetwork: true\n"
    "      hostPID: true\n"
    "---\n"
    "apiVersion: rbac.authorization.k8s.io/v1\n"
    "kind: RoleBinding\n"
    "metadata:\n"
    "  name: pwn\n"
    "  namespace: rig-prd-demo\n"
    "roleRef:\n"
    "  apiGroup: rbac.authorization.k8s.io\n"
    "  kind: ClusterRole\n"
    "  name: cluster-admin\n"
    "subjects:\n"
    "- kind: ServiceAccount\n"
    "  name: default\n"
    "  namespace: rig-prd-demo\n"
    "---\n"
    "apiVersion: v1\n"
    "kind: ConfigMap\n"
    "metadata:\n"
    "  name: rest\n"
    "spec:\n"
    "  a:\n"
    "    b:"
)


class TestHetSecretNameKanNietUitZijnRegelBreken:
    """Het vierde veld van ``RegistryEntry``, en het laatste zonder patroon.

    ``name`` en ``upstream`` kregen er een voor precies deze aanvalsklasse; ``secretName``
    bleef achter terwijl hij in ``deployment.yaml.jinja`` ongequote achter ``- name:``
    terechtkomt. Gemeten op de branch voor deze reparatie: pydantic accepteerde de payload,
    alle drie de poorten lieten hem door, ``resolve_deployment_component_image`` gaf hem
    door en de gerenderde Deployment droeg DRIE documenten, met ``hostNetwork``/``hostPID``
    aan en een RoleBinding naar ``cluster-admin``.

    Het gat bestond ook op de basis (b1b7f8a1) en is dus niet door deze PR geintroduceerd,
    maar dit is de PR die het veld zijn nieuwe model geeft.
    """

    def test_de_injectiepayload_sneuvelt_op_de_poorten(self) -> None:
        with pytest.raises((ProjectSchemaError, ProjectIntegrityError)) as excinfo:
            poorten(_project({"name": "eigen", "upstream": "ghcr.io", "secretName": SECRET_NAME_INJECTIE}))
        assert "image-registries" in str(excinfo.value)

    @pytest.mark.parametrize(
        "secret_name",
        [
            "rig-robot-pull-secret\n      hostNetwork: true",  # een regeleinde alleen al
            "rig-robot-pull-secret\n",  # een afsluitende newline: de val van een ``$``-anker
            "Rig-Robot-Pull-Secret",  # kubernetes accepteert geen hoofdletters
            "met spatie",
            "-begint-met-streepje",
            "eindigt-op-streepje-",
            "onder_streep",
            "a" * 254,  # boven de RFC-1123-grens
            "",
        ],
    )
    def test_geweigerde_secretnamen(self, secret_name: str) -> None:
        with pytest.raises((ProjectSchemaError, ProjectIntegrityError)):
            poorten(_project({"name": "eigen", "upstream": "ghcr.io", "secretName": secret_name}))

    @pytest.mark.parametrize(
        "secret_name",
        [
            "rig-robot-pull-secret",  # de waarde die dp-bn7 echt draagt, dus de migratie 2.8 -> 2.9
            "ghcr-rig-robot-pull-secret",
            "codeoverheid-rig-demo-robot-pull-secret",
            "s",
            "punt.in.de.naam",
            "a" * 253,
        ],
    )
    def test_toegestane_secretnamen(self, secret_name: str) -> None:
        poorten(_project({"name": "eigen", "upstream": "ghcr.io", "secretName": secret_name}))

    def test_de_ownership_toets_leest_de_toegestane_vorm_nog(self) -> None:
        """``validate_registry_entry_ownership`` haalt er ``-robot-pull-secret`` af om de
        organisatie te vinden. Een RFC-1123-patroon laat die vorm heel, dus die regel meet
        na deze reparatie nog steeds wat hij mat."""
        from opi.services.catalog.image_registries.config_model import SECRET_NAME_PATTERN
        from opi.services.catalog.image_registries.ownership import PULL_SECRET_POSTFIX

        naam = f"codeoverheid-rig-anderproject-{PULL_SECRET_POSTFIX}"
        assert re.match(SECRET_NAME_PATTERN, naam)
        assert naam.removesuffix(f"-{PULL_SECRET_POSTFIX}") == "codeoverheid-rig-anderproject"

    def test_de_api_weigert_de_payload_aan_de_deur(self) -> None:
        """``POST /projects/{p}/registries/by-secret`` schrijft rechtstreeks tegen dit
        model. Het droeg wel een ``max_length`` en geen patroon, dus alles wat binnen 253
        tekens past kwam erdoor."""
        import pydantic
        from opi.api.router import AddRegistryBySecretRequest

        AddRegistryBySecretRequest(name="eigen", url="ghcr.io", secretName="rig-robot-pull-secret")
        for kwaad in ("rig-robot-pull-secret\n      hostNetwork: true\n---\nkind: RoleBinding", "Hoofdletters"):
            with pytest.raises(pydantic.ValidationError):
                AddRegistryBySecretRequest(name="eigen", url="ghcr.io", secretName=kwaad)

    def test_het_gerenderde_manifest_blijft_een_document(self) -> None:
        """Het tweede, onafhankelijke slot: ook een waarde die BINNENDOOR reist -- een
        bestaand projectbestand, de migratie 2.8 -> 2.9, die ``secretName`` allebei
        ongetoetst overzetten -- blijft binnen zijn scalar.

        Zonder ``| yaml_scalar`` gaf deze render drie documenten, waarvan het tweede de
        RoleBinding naar ``cluster-admin`` was.
        """
        from test_golden_manifests import _deployment_vars

        image = "ghcr.io/team/app:1.0"
        uitvoer = render_template(
            "deployment.yaml.jinja",
            _deployment_vars(imageURL=image, imagePullSecretsMap={image: SECRET_NAME_INJECTIE}),
        )
        documenten = [doc for doc in yaml.safe_load_all(uitvoer) if doc]
        assert [doc["kind"] for doc in documenten] == ["Deployment"]
        pod_spec = documenten[0]["spec"]["template"]["spec"]
        assert "hostNetwork" not in pod_spec
        assert "hostPID" not in pod_spec
        assert pod_spec["imagePullSecrets"] == [{"name": SECRET_NAME_INJECTIE}]

    def test_de_postgres_cluster_render_draagt_hetzelfde_slot(self) -> None:
        """Dezelfde secretnaam komt via ``registry:`` in de config van
        ``namespace-postgresql-database`` in een tweede sjabloon terecht
        (``project_manager.py``: ``image_pull_secrets_map[database_image] = secretName``).
        """
        image = "ghcr.io/team/postgres:17"
        uitvoer = render_template(
            "postgresql-cluster.yaml.jinja",
            {
                "project_name": "demo",
                "infrastructure_namespace": "rig-prd-demo",
                "database_config": {
                    "image": image,
                    "instances": 1,
                    "storage": "1Gi",
                    "resources": {
                        "requests": {"memory": "256Mi", "cpu": "100m"},
                        "limits": {"memory": "512Mi", "cpu": "500m"},
                    },
                },
                "storage_class": "standard",
                "imagePullSecretsMap": {image: SECRET_NAME_INJECTIE},
            },
        )
        documenten = [doc for doc in yaml.safe_load_all(uitvoer) if doc]
        assert [doc["kind"] for doc in documenten] == ["Cluster"]
        assert documenten[0]["spec"]["imagePullSecrets"] == [{"name": SECRET_NAME_INJECTIE}]

    def test_het_gecommitte_fragment_draagt_het_patroon(self) -> None:
        """Zelfde drift-lock als bij ``upstream``: het fragment komt uit het model."""
        import json
        from pathlib import Path

        from opi.services.catalog.image_registries.config_model import SECRET_NAME_PATTERN

        fragment = json.loads(
            (
                Path(__file__).resolve().parent.parent
                / "opi/services/catalog/image_registries/image-registries.v1.0.json"
            ).read_text()
        )
        secret_name = fragment["$defs"]["RegistryEntry"]["properties"]["secretName"]
        assert secret_name["anyOf"][0]["pattern"] == SECRET_NAME_PATTERN
        assert secret_name["anyOf"][0]["maxLength"] == 253
