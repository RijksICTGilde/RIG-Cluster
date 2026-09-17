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

import base64
import copy
import json
import os
import re
from pathlib import Path
from typing import Any

import pydantic
import pytest
import yaml
from opi.api.router import AddRegistryByCredentialsRequest, AddRegistryBySecretRequest
from opi.core.project_schema import ProjectIntegrityError, ProjectSchemaError, validate_project_schema
from opi.forms.editables.processor import EditableFormProcessor
from opi.forms.editables.rendered_sequences import GERENDERDE_REEKSEN_VELD
from opi.generation.manifests import ManifestGenerator, render_template
from opi.manager.project_validation import find_plaintext_service_config_violations, validate_service_configs
from opi.services.catalog.base import ConfigLayer, ProjectManifestContext
from opi.services.catalog.image_registries import ImageRegistriesService
from opi.services.catalog.image_registries.config_model import SECRET_NAME_PATTERN, UPSTREAM_PATTERN, RegistryEntry
from opi.services.catalog.image_registries.editables import REGISTRY_NAME_EDITABLE, REGISTRY_UPSTREAM_EDITABLE
from opi.services.catalog.image_registries.naming import PULL_USERNAME_PLACEHOLDER
from opi.services.catalog.image_registries.ownership import PULL_SECRET_POSTFIX
from opi.services.project_store import GitProjectStore
from opi.services.registry import get_service
from opi.services.schema_migration import relocate_registries_to_service
from opi.services.services_enums import ServiceType
from pydantic import ValidationError

from test_golden_manifests import _deployment_vars

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
AGE_ENTRY = {"name": "versleuteld", "upstream": "ghcr.io", "username": "u", "password": AGE_BLOCK}

#: Een entry draagt een gebruikersnaam plus token of een secretName (``RegistryEntry``); de
#: tests hier gaan over iets anders en geven daarom gewoon inloggegevens mee.
CREDS = {"username": "u", "password": "plain:een-token"}

#: De andere manier om te pullen: een secret dat het platform zelf neerzet.
SECRET = {"secretName": "rig-robot-pull-secret"}


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


def _leeg_project() -> dict[str, Any]:
    """Een geldig projectbestand met een lege registrylijst, klaar voor een inzending."""
    project = _project({"name": "eigen", "upstream": "ghcr.io/team", **SECRET})
    project["services"][0]["config"]["registries"] = []
    return project


def _inzending(registry: dict[str, Any]) -> dict[str, Any]:
    """Wat de modaal post: de dienstconfig plus de lijst met gerenderde reeksen."""
    return {
        "_services-config": {"image-registries": {"config": {"registries": [registry]}}},
        GERENDERDE_REEKSEN_VELD: ["_services-config/image-registries/config/registries"],
    }


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
            poorten(_project({"name": "eigen", "upstream": upstream, **CREDS}))


class TestDeUpstreamKanNietUitZijnScalarBreken:
    def test_de_injectiepayload_sneuvelt_op_de_poorten(self) -> None:
        """Dit is de meting van de bevinding: op de branch zonder patroon kwam deze
        payload door alle drie de validaties heen."""
        with pytest.raises((ProjectSchemaError, ProjectIntegrityError)) as excinfo:
            poorten(_project({"name": "eigen", "upstream": YAML_INJECTIE_UPSTREAM, **CREDS}))
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
            poorten(_project({"name": "eigen", "upstream": upstream, **CREDS}))

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
        poorten(_project({"name": "eigen", "upstream": upstream, **CREDS}))

    def test_het_formulier_draait_dezelfde_regel_als_de_api(self) -> None:
        """Geen validator die alleen het formulier draait: de editable WIJST naar het model.

        Zou iemand de regel in het formulier opnieuw opschrijven, dan lopen de twee uit
        elkaar en accepteert de API wat het formulier weigert, precies hoe deze payload
        langs de UpstreamValidator kwam.

        Een geplakte URL is sinds RC-187 geen fout meer maar invoer die wordt omgezet, en
        ook dat doen de twee samen: de omzetting hangt aan het VELD in het model, dus de
        toets die het formulier eruit bouwt draagt hem mee.
        """
        validator = REGISTRY_UPSTREAM_EDITABLE.validator
        assert validator is not None
        assert validator.validate("code.overheid.nl/robbert.uittenbroek") == []
        assert validator.validate(YAML_INJECTIE_UPSTREAM) != []
        assert validator.validate("ghcr") != []
        assert validator.validate("https://ghcr.io") == []

    def test_de_api_weigert_de_payload_aan_de_deur(self) -> None:
        """``POST /projects/{p}/registries/by-credentials`` was de gemeten ingang."""
        AddRegistryByCredentialsRequest(name="eigen", url="ghcr.io", username="u", password="t")
        with pytest.raises(pydantic.ValidationError):
            AddRegistryByCredentialsRequest(name="eigen", url=YAML_INJECTIE_UPSTREAM, username="u", password="t")

    def test_de_api_laat_de_gebruikersnaam_weg_maar_niet_het_token(self) -> None:
        """De gebruikersnaam is optioneel op elke schrijfweg, dus ook aan de API-deur."""
        verzoek = AddRegistryByCredentialsRequest(name="eigen", url="ghcr.io/team", password="t")
        assert verzoek.username is None
        with pytest.raises(pydantic.ValidationError):
            AddRegistryByCredentialsRequest(name="eigen", url="ghcr.io/team", username="u")

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
                            "config": {"registries": [{"name": "eigen", "upstream": YAML_INJECTIE_UPSTREAM, **CREDS}]},
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

    def test_platte_tekst_op_de_deploymentlaag_wordt_ook_gevonden(self) -> None:
        """Het model geldt ook op de deploymentlaag, dus daar kan het token ook leesbaar staan.

        De melding wijst het blok op positie aan; tweede deployment, derde dienst.
        """
        project = _project({"name": "eigen", "upstream": "ghcr.io"})
        lek = {"name": "eigen", "upstream": "ghcr.io", "username": "u", "password": "ghp_KLARTEKST"}
        project["deployments"] = [
            {"name": "acc"},
            {
                "name": "prod",
                "services": [
                    "clone",
                    {"name": "clone", "config": {"generation": 1}},
                    {"name": "image-registries", "config": {"registries": [AGE_ENTRY, lek]}},
                ],
            },
        ]
        assert find_plaintext_service_config_violations(project) == [
            "deployments/1/services/2/config/registries/1/password"
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
        store = GitProjectStore(working_dir="/tmp/unused-by-this-test")
        leaked = _project({"name": "eigen", "upstream": "ghcr.io", "username": "u", "password": "ghp_KLARTEKST"})

        with pytest.raises(ProjectSchemaError, match="AGE-versleuteld"):
            await store._validate(leaked, enforce=False)

    def test_een_age_blok_is_geen_overtreding(self) -> None:
        project = _project({"name": "eigen", "upstream": "ghcr.io", "username": "u", "password": AGE_BLOCK})
        assert find_plaintext_service_config_violations(project) == []

    def test_een_leeg_token_is_geen_overtreding(self) -> None:
        """Een ontbrekend token is geen PLATTE TEKST; dat hij ontbreekt weigert het model zelf."""
        assert find_plaintext_service_config_violations(_project({"name": "eigen", "upstream": "ghcr.io"})) == []

    def test_de_controle_leest_het_patroon_uit_het_model(self) -> None:
        """Afgeleid van het schema, niet van een handgeschreven veldenlijst.

        Haal het AGE-patroon van ``password`` weg en deze controle vindt niets meer, dat
        is precies wat er bij de verhuizing gebeurde.
        """
        pattern = str(RegistryEntry.model_json_schema()["properties"]["password"]["anyOf"][0]["pattern"])
        assert "BEGIN AGE ENCRYPTED FILE" in pattern


class TestDeInvoerhulpRepareertGeenOpgeslagenBestand:
    """De omzetting is een hulp aan de DEUR, geen versoepeling van de opgeslagen vorm.

    Valideren schrijft niet terug, dus zou de hele-bestandspoort ook normaliseren, dan kwam
    een opgeslagen ``https://ghcr.io`` door de poort en bleef hij ongewijzigd in het
    bestand staan -- waarna ``normalize_prefix`` hem nooit matcht en de registry stil niet
    meer geldt in plaats van luid geweigerd te worden.
    """

    @pytest.mark.parametrize(
        "upstream",
        ["https://ghcr.io", "GHCR.IO", "ghcr.io/app:1.0", "ghcr.io\n"],
    )
    def test_de_poorten_weigeren_hem_nog_steeds(self, upstream: str) -> None:
        with pytest.raises((ProjectSchemaError, ProjectIntegrityError)):
            poorten(_project({"name": "eigen", "upstream": upstream, **CREDS}))

    @pytest.mark.parametrize(
        ("geplakt", "upstream"),
        [
            ("https://code.overheid.nl/robbert.uittenbroek/-/packages", "code.overheid.nl/robbert.uittenbroek"),
            ("https://github.com/orgs/rijksictgilde/packages", "ghcr.io/rijksictgilde"),
            ("https://hub.docker.com/r/bitnami/nginx", "docker.io/bitnami"),
            ("https://gitlab.com/groep/project/container_registry", "registry.gitlab.com/groep/project"),
            ("code.overheid.nl/team/app:1.2", "code.overheid.nl/team"),
            ("HTTPS://GHCR.IO/", "ghcr.io"),
        ],
    )
    def test_aan_de_deur_wordt_hij_wel_omgezet(self, geplakt: str, upstream: str) -> None:
        """Zonder validatiecontext -- het formulier en de API -- is dit invoer die wij
        onder water goed zetten."""
        assert RegistryEntry(name="eigen", upstream=geplakt, **CREDS).upstream == upstream

    def test_een_onbekende_vorm_wordt_niet_stil_verminkt(self) -> None:
        """Wat we niet herkennen laten we met rust, zodat het patroon hem afwijst in plaats
        van er iets van te maken dat ergens anders heen wijst."""
        with pytest.raises(ValidationError):
            RegistryEntry(name="eigen", upstream=YAML_INJECTIE_UPSTREAM, **CREDS)


class TestHetPatroonStaatOpEenPlek:
    def test_de_gecommitte_fragment_draagt_hetzelfde_patroon(self) -> None:
        """Het schemafragment wordt uit het model gerenderd; drift is uitgesloten."""
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
            poorten(_project({"name": name, "upstream": "ghcr.io", **CREDS}))

    @pytest.mark.parametrize("name", ["code-overheid", "ghcr", "a", "registry-2"])
    def test_toegestane_namen(self, name: str) -> None:
        poorten(_project({"name": name, "upstream": "ghcr.io", **CREDS}))

    def test_het_api_model_draagt_dezelfde_regel(self) -> None:
        """Het endpoint schrijft rechtstreeks tegen dit model; zonder het patroon daar komt
        een naam die het formulier weigert alsnog het projectbestand in."""
        for model in (AddRegistryBySecretRequest, AddRegistryByCredentialsRequest):
            velden = {"name": "Hoofdletters", "url": "ghcr.io", "secretName": "s", "username": "u", "password": "p"}
            with pytest.raises(ValidationError):
                model(**velden)

    def test_het_formulier_en_het_model_wijzen_naar_dezelfde_regel(self) -> None:
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
        naam = f"codeoverheid-rig-anderproject-{PULL_SECRET_POSTFIX}"
        assert re.match(SECRET_NAME_PATTERN, naam)
        assert naam.removesuffix(f"-{PULL_SECRET_POSTFIX}") == "codeoverheid-rig-anderproject"

    def test_de_api_weigert_de_payload_aan_de_deur(self) -> None:
        """``POST /projects/{p}/registries/by-secret`` schrijft rechtstreeks tegen dit
        model. Het droeg wel een ``max_length`` en geen patroon, dus alles wat binnen 253
        tekens past kwam erdoor."""
        AddRegistryBySecretRequest(name="eigen", url="ghcr.io", secretName="rig-robot-pull-secret")
        for kwaad in ("rig-robot-pull-secret\n      hostNetwork: true\n---\nkind: RoleBinding", "Hoofdletters"):
            with pytest.raises(pydantic.ValidationError):
                AddRegistryBySecretRequest(name="eigen", url="ghcr.io", secretName=kwaad)

    def test_het_gerenderde_manifest_blijft_een_document(self) -> None:
        """Het tweede, onafhankelijke slot: ook een waarde die BINNENDOOR reist (een
        bestaand projectbestand, de migratie 2.8 -> 2.9, die ``secretName`` allebei
        ongetoetst overzetten) blijft binnen zijn scalar.

        Zonder ``| yaml_scalar`` gaf deze render drie documenten, waarvan het tweede de
        RoleBinding naar ``cluster-admin`` was.
        """
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
        fragment = json.loads(
            (
                Path(__file__).resolve().parent.parent
                / "opi/services/catalog/image_registries/image-registries.v1.0.json"
            ).read_text()
        )
        secret_name = fragment["$defs"]["RegistryEntry"]["properties"]["secretName"]
        assert secret_name["anyOf"][0]["pattern"] == SECRET_NAME_PATTERN
        assert secret_name["anyOf"][0]["maxLength"] == 253


class TestEenRegistryHeeftEenManierOmTePullen:
    """Een entry draagt OF een ``secretName`` OF een token, nooit geen van beide en nooit
    allebei.

    Zonder deze regel kwam een entry zonder token door formulier en API, schreef de backend
    stil geen pull-secret, en merkte de afnemer het pas aan een pod die niet kon pullen.
    Gemeten op de save-poort, want daar komen formulier en API allebei langs.

    De gebruikersnaam hoort NIET bij de regel (RC-187): wat hij betekent verschilt per
    registry, dus hij is optioneel. Een registry die er wel een eist houdt niets hier tegen;
    de tokentoets vangt het alleen in een formulierflow met het registryblok, tegen de images
    in de samengevoegde data van die flow (niet in de componentmodal, niet via de API).
    """

    @pytest.mark.parametrize(
        "manier",
        [
            pytest.param({"username": "u", "password": AGE_BLOCK}, id="gebruikersnaam-plus-token"),
            pytest.param({"password": AGE_BLOCK}, id="alleen-token"),
            pytest.param({"username": "", "password": AGE_BLOCK}, id="lege-gebruikersnaam-plus-token"),
            pytest.param({"secretName": "rig-robot-pull-secret"}, id="secretname"),
        ],
    )
    def test_elke_geldige_vorm_komt_door_de_poorten(self, manier: dict[str, str]) -> None:
        poorten(_project({"name": "eigen", "upstream": "ghcr.io", **manier}))

    @pytest.mark.parametrize(
        ("manier", "melding"),
        [
            pytest.param({}, "Vul een token in", id="niets"),
            pytest.param({"username": "u"}, "Vul een token in bij de gebruikersnaam", id="alleen-gebruikersnaam"),
        ],
    )
    def test_zonder_token_wordt_hij_geweigerd(self, manier: dict[str, str], melding: str) -> None:
        """De melding noemt wat er MIST: bij een entry met een gebruikersnaam vraagt hij
        alleen het token, niet ook de naam die er al staat."""
        with pytest.raises(ProjectIntegrityError, match=melding):
            poorten(_project({"name": "eigen", "upstream": "ghcr.io", **manier}))

    @pytest.mark.parametrize(
        "manier",
        [
            pytest.param({"username": "u", "password": AGE_BLOCK}, id="secretname-plus-beide"),
            pytest.param({"username": "u"}, id="secretname-plus-gebruikersnaam"),
            pytest.param({"password": AGE_BLOCK}, id="secretname-plus-token"),
        ],
    )
    def test_de_twee_vormen_mengen_wordt_geweigerd(self, manier: dict[str, str]) -> None:
        with pytest.raises(ProjectIntegrityError, match="niet allebei"):
            poorten(_project({"name": "eigen", "upstream": "ghcr.io", **SECRET, **manier}))

    def test_de_melding_noemt_het_token_niet(self) -> None:
        """De weigering gaat als melding naar het scherm; het token hoort er niet in.

        Gemeten op de mengvorm, want dat is de weigering die een token IN de entry heeft:
        een entry met alleen een token is sinds RC-187 juist geldig.
        """
        with pytest.raises(ProjectIntegrityError) as excinfo:
            poorten(_project({"name": "eigen", "upstream": "ghcr.io", **SECRET, "password": "plain:ghp_GEHEIM"}))
        assert "ghp_GEHEIM" not in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_het_formulier_zonder_gebruikersnaam_komt_er_wel_door(self) -> None:
        """De spiegel van de test hieronder: het token is de eis, de gebruikersnaam niet."""
        section = get_service(ServiceType.IMAGE_REGISTRIES).config_form_section(ConfigLayer.PROJECT)
        assert section is not None
        submitted, errors = await EditableFormProcessor().process_json_submission(
            _inzending({"name": "eigen", "upstream": "ghcr.io/team", "username": "", "password": "plain:een-token"}),
            section.editables,
            copy.deepcopy(_leeg_project()),
            edit_mode=True,
        )
        assert not errors, errors
        await GitProjectStore(working_dir="/tmp/unused-by-this-test")._validate(submitted, enforce=False)

    @pytest.mark.asyncio
    async def test_het_formulier_zonder_token_sneuvelt_op_de_store(self) -> None:
        """De portaalroute: de inzending verwerken zoals de modaal doet, dan de save-poort.

        Het formulier heeft geen ``secretName``-veld, dus wie daar het token leeg laat heeft
        geen van beide vormen. De weigering komt van de store, dezelfde poort als de API.
        """
        section = get_service(ServiceType.IMAGE_REGISTRIES).config_form_section(ConfigLayer.PROJECT)
        assert section is not None
        inzending = {
            "_services-config": {
                "image-registries": {
                    "config": {"registries": [{"name": "eigen", "upstream": "ghcr.io/team", "username": "u"}]}
                }
            },
            GERENDERDE_REEKSEN_VELD: ["_services-config/image-registries/config/registries"],
        }
        project = _project({"name": "eigen", "upstream": "ghcr.io/team", **SECRET})
        project["services"][0]["config"]["registries"] = []
        submitted, errors = await EditableFormProcessor().process_json_submission(
            inzending, section.editables, copy.deepcopy(project), edit_mode=True
        )
        assert not errors, errors
        assert submitted["services"][0]["config"]["registries"] == [
            {"name": "eigen", "upstream": "ghcr.io/team", "username": "u"}
        ]

        with pytest.raises(ProjectIntegrityError, match="Vul een token in bij de gebruikersnaam"):
            await GitProjectStore(working_dir="/tmp/unused-by-this-test")._validate(submitted, enforce=True)


class TestDeGebruikersnaamBlijftLeegInHetProjectbestand:
    """De harde eis bij RC-187: de plaatshouder wordt BEREKEND, niet opgeslagen.

    Het projectbestand draagt alleen wat de afnemer heeft ingevuld -- dezelfde regel als bij
    de RCR-URL en de secretnaam. Laat de afnemer de gebruikersnaam leeg, dan blijft hij leeg
    in de config; ``PULL_USERNAME_PLACEHOLDER`` ontstaat pas als de dockerconfigjson wordt
    gebouwd. Gemeten op de weg waarlangs hij er anders in zou sluipen: het formulier, de
    save-poort en de migratie.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("ingevuld", ["", None], ids=["leeg-veld", "veld-weggelaten"])
    async def test_na_een_save_staat_er_nog_steeds_geen_gebruikersnaam(self, ingevuld: str | None) -> None:
        section = get_service(ServiceType.IMAGE_REGISTRIES).config_form_section(ConfigLayer.PROJECT)
        assert section is not None
        registry: dict[str, Any] = {"name": "eigen", "upstream": "ghcr.io/team", "password": "plain:een-token"}
        if ingevuld is not None:
            registry["username"] = ingevuld
        submitted, errors = await EditableFormProcessor().process_json_submission(
            _inzending(registry), section.editables, copy.deepcopy(_leeg_project()), edit_mode=True
        )
        assert not errors, errors

        await GitProjectStore(working_dir="/tmp/unused-by-this-test")._validate(submitted, enforce=False)

        opgeslagen = submitted["services"][0]["config"]["registries"][0]
        assert not opgeslagen.get("username"), opgeslagen
        assert PULL_USERNAME_PLACEHOLDER not in json.dumps(submitted)

    def test_het_gegenereerde_secret_draagt_wel_een_volledig_paar(self) -> None:
        """De andere helft van de eis: leeg in het bestand, compleet in het manifest."""
        registry = {"name": "eigen", "upstream": "ghcr.io/team", "password": "plain:een-token"}
        ctx = ProjectManifestContext(
            project_name="demo",
            project_data=_project(registry),
            cluster="sandboxed-local",
            namespace="rig-prd-demo",
        )
        spec = ImageRegistriesService().contribute_project_manifests(ctx)[0]
        config = json.loads(spec.values["secret_pairs"][".dockerconfigjson"])
        auth = base64.b64decode(config["auths"]["ghcr.io/team"]["auth"]).decode()
        assert auth == f"{PULL_USERNAME_PLACEHOLDER}:een-token"

    def test_de_migratie_vult_hem_niet_aan(self) -> None:
        """De migratie 2.8 -> 2.9 verhuist de sleutel; aanvullen doet ze niet."""
        oud = {
            "schema-version": 2.8,
            "name": "demo",
            "registries": [{"name": "eigen", "url": "ghcr.io/team", "password": "plain:een-token"}],
        }
        relocate_registries_to_service(oud)
        verhuisd = oud["services"][0]["config"]["registries"][0]
        assert "username" not in verhuisd, verhuisd
