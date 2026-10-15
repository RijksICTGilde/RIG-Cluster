"""De koppeling tussen de JupyterHub-POC en ZAD (RC-258).

``images/jupyterhub-hub/jupyterhub_config.py`` leest alles wat per project verschilt uit
env vars, en ``projects/ideas/jupyterhub-poc.yaml`` is wat die vars vult. Dat contract is
nergens afdwingbaar: een hernoemde variabele, een andere servicenaam of een verschoven
poort valt pas tien minuten later om in een pod op de sandbox. Deze tests meten het hier.
"""

import os
from pathlib import Path
from typing import Any

import pytest
from opi.core.project_schema import validate_project_schema
from opi.forms.editables.fields.domains import WIZARD_DEPLOYMENT_NAME_EDITABLE
from opi.manager.project_validation import validate_service_configs
from opi.services.catalog.keycloak.variables import KeycloakVariables
from opi.services.catalog.minio.variables import MinIOVariables
from opi.services.catalog.persistent_storage.variables import PersistentStorageVariables
from opi.services.catalog.publish_on_web.variables import WebVariables
from opi.services.services import service_entry_config, service_entry_name
from opi.utils.naming import generate_unique_name
from opi.utils.secrets import MinIOSecret
from ruamel.yaml import YAML

REPO_ROOT = Path(__file__).resolve().parents[3]
PROJECT_FILE = REPO_ROOT / "projects" / "ideas" / "jupyterhub-poc.yaml"
HUB_CONFIG = REPO_ROOT / "images" / "jupyterhub-hub" / "jupyterhub_config.py"


class _Recorder:
    """Legt ``c.KubeSpawner.image = ...`` vast zonder traitlets erbij te halen."""

    def __init__(self) -> None:
        self._sections: dict[str, _Section] = {}
        #: De namen op moduleniveau van de config, na het uitvoeren ervan.
        self.module: dict[str, Any] = {}

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return self._sections.setdefault(name, _Section())

    def section(self, name: str) -> dict[str, Any]:
        return dict(self._sections[name].__dict__)


class _Section:
    pass


def _project() -> dict[str, Any]:
    yaml = YAML(typ="safe")
    with PROJECT_FILE.open(encoding="utf-8") as handle:
        return yaml.load(handle)


def _hub_component(project: dict[str, Any]) -> dict[str, Any]:
    return next(component for component in project["components"] if component["name"] == "hub")


def _platform_env() -> dict[str, str]:
    """Wat de diensten van het project op het component zetten.

    Uit de ``*Variables``-enums zelf, zodat een hernoeming aan de kant van een dienst deze
    test rood maakt in plaats van de hub op de sandbox.
    """
    env = {
        KeycloakVariables.URL.value.name: "https://keycloak.sandbox.rijksapp.dev",
        KeycloakVariables.REALM.value.name: "jupyterhub-poc-sandboxed-local",
        KeycloakVariables.CLIENT_ID.value.name: "jupyterhub-poc-productie-hub",
        KeycloakVariables.CLIENT_SECRET.value.name: "geheim",
        WebVariables.PUBLIC_HOSTNAME.value.name: "jupyterhub-poc.sandbox.rijksapp.dev",
        PersistentStorageVariables.DATA_PATH.value.name: "/data",
    }
    for variable in (MinIOVariables.HOST, MinIOVariables.BUCKET_NAME):
        env[variable.value.name] = f"waarde-voor-{variable.value.name.lower()}"
    return env


def _load_hub_config(env: dict[str, str]) -> _Recorder:
    recorder = _Recorder()
    namespace: dict[str, Any] = {"get_config": lambda: recorder, "__name__": "jupyterhub_config"}
    source = HUB_CONFIG.read_text(encoding="utf-8")
    previous = dict(os.environ)
    os.environ.clear()
    os.environ.update(env)
    try:
        exec(compile(source, str(HUB_CONFIG), "exec"), namespace)  # noqa: S102
    finally:
        os.environ.clear()
        os.environ.update(previous)
    recorder.module.update(namespace)
    return recorder


def _full_env() -> dict[str, str]:
    env = _platform_env()
    env.update({key: str(value) for key, value in _hub_component(_project())["user-env-vars"].items()})
    return env


def test_project_file_passes_the_current_schema() -> None:
    """Het POC-projectbestand moet door dezelfde poort als elk ander projectbestand."""
    project = _project()
    validate_project_schema(project)
    validate_service_configs(project)


def test_hub_config_starts_on_exactly_what_the_project_file_and_the_services_provide() -> None:
    """Geen env var die niemand zet: de hub weigert te starten op een lege waarde."""
    _load_hub_config(_full_env())


@pytest.mark.parametrize("key", sorted(_hub_component(_project())["user-env-vars"]))
def test_every_user_env_var_in_the_project_file_is_one_the_hub_config_reads(key: str) -> None:
    """Een sleutel in het projectbestand die de config niet leest, is dode configuratie."""
    assert key in HUB_CONFIG.read_text(encoding="utf-8"), (
        f"{key} staat in projects/ideas/jupyterhub-poc.yaml maar wordt door de hub niet gelezen"
    )


def test_hub_points_at_its_own_clusterip_service() -> None:
    """``hub_connect_url`` moet de Service zijn die OPI voor dit component maakt.

    Staat hier een andere naam, dan bereikt een user-pod de hub-API niet en blijft elke
    spawn hangen tot de start-timeout.
    """
    project = _project()
    deployment = project["deployments"][0]
    expected = generate_unique_name(deployment["name"], deployment["components"][0]["reference"])

    assert _hub_component(project)["user-env-vars"]["JUPYTERHUB_POC_HUB_SERVICE"] == expected

    hub = _load_hub_config(_full_env()).section("JupyterHub")
    assert hub["hub_connect_url"] == f"http://{expected}:8081"


def test_the_reference_file_names_the_deployment_the_way_the_wizard_does() -> None:
    """Het bestand beschrijft wat ZAD aanmaakt, en ZAD noemt de deployment ``productie``.

    Staat er iets anders, dan wijzen HUB_SERVICE en DEPLOYMENT_LABEL naar een Service en
    een podlabel die niet bestaan. De twee toetsen hieronder zien dat niet: die lezen de
    deploymentnaam uit datzelfde bestand.
    """
    assert _project()["deployments"][0]["name"] == WIZARD_DEPLOYMENT_NAME_EDITABLE.default


def test_user_pods_carry_the_deployment_label_the_tenant_netpol_selects_on() -> None:
    """Zonder dit label valt een user-pod buiten elke NetworkPolicy en is hij default-allow."""
    project = _project()
    deployment_name = project["deployments"][0]["name"]

    assert _hub_component(project)["user-env-vars"]["JUPYTERHUB_POC_DEPLOYMENT_LABEL"] == deployment_name

    labels = _load_hub_config(_full_env()).section("KubeSpawner")["extra_labels"]
    assert labels == {"deployment": deployment_name}


def test_a_rollout_of_the_hub_leaves_the_running_user_pods_alone() -> None:
    """``cleanup_servers`` staat standaard op True, en dan stopt afsluiten elke server.

    Elke rollout van het hubcomponent zou daarmee de pod van iedere gebruiker weghalen,
    precies wat assertie 9 uitsluit. Het ontbreken van de regel is onzichtbaar.
    """
    assert _load_hub_config(_full_env()).section("JupyterHub")["cleanup_servers"] is False


def test_the_minio_names_the_hub_passes_on_are_names_the_service_really_sets() -> None:
    """Een naam die de dienst niet zet, geeft een user-pod zonder toegang tot de bucket.

    ``OBJECT_STORE_URL`` en ``OBJECT_STORE_ENDPOINT_URL`` zijn geen ``VariableDefinition``
    maar worden in ``MinIOSecret`` berekend, dus de enum alleen is hier niet de waarheid.
    """
    secret = MinIOSecret(host="minio", port=9000, access_key="a", secret_key="s", bucket_name="b", region="eu")
    passthrough = set(_load_hub_config(_full_env()).module["_PASSTHROUGH_ENV"])

    assert passthrough
    assert passthrough <= set(secret.to_k8s_secret_data())


def test_user_pods_never_carry_the_labels_the_hub_itself_is_selected_on() -> None:
    """De valkuil: Service en Deployment van de hub selecteren op ``app`` + ``component``.

    Een user-pod met die labels komt in de loadbalancing van de hub terecht.
    """
    labels = _load_hub_config(_full_env()).section("KubeSpawner")["extra_labels"]
    assert "app" not in labels
    assert "component" not in labels


def test_the_published_port_is_the_proxy_and_the_second_port_is_the_hub_api() -> None:
    """``application_port = inbound_ports[0]`` bepaalt wat de ingress krijgt.

    Staat 8081 vooraan, dan publiceert publish-on-web de hub-API naar het web en is de
    proxy onbereikbaar.
    """
    inbound = _hub_component(_project())["ports"]["inbound"]
    hub = _load_hub_config(_full_env()).section("JupyterHub")

    assert hub["bind_url"] == f"http://:{inbound[0]}"
    assert hub["hub_bind_url"] == f"http://0.0.0.0:{inbound[1]}"


def test_user_pods_get_no_serviceaccount_token() -> None:
    """Een user-pod draait code van een gebruiker en hoort niets van Kubernetes te kunnen."""
    spawner = _load_hub_config(_full_env()).section("KubeSpawner")
    assert spawner["automount_service_account_token"] is False
    assert spawner["container_security_context"]["runAsNonRoot"] is True
    assert spawner["container_security_context"]["capabilities"] == {"drop": ["ALL"]}
    assert spawner["container_security_context"]["allowPrivilegeEscalation"] is False
    assert spawner["container_security_context"]["seccompProfile"] == {"type": "RuntimeDefault"}


#: Variabelen met een bewuste default in de config: de hub start er zonder. Alles wat hier
#: NIET in staat, moet de hub laten weigeren; een nieuwe default valt daarmee op.
OPTIONAL_ENV = frozenset(
    {
        PersistentStorageVariables.DATA_PATH.value.name,
        MinIOVariables.HOST.value.name,
        MinIOVariables.BUCKET_NAME.value.name,
        "JUPYTERHUB_POC_USER_STORAGE",
        "JUPYTERHUB_POC_USER_CPU_LIMIT",
        "JUPYTERHUB_POC_USER_MEM_LIMIT",
        "JUPYTERHUB_POC_CULL_TIMEOUT",
        "JUPYTERHUB_POC_ADMINS",
    }
)


@pytest.mark.parametrize("missing", sorted(_full_env()))
def test_a_value_the_hub_cannot_do_without_is_refused_and_the_rest_has_a_default(missing: str) -> None:
    """Elke variabele is afzonderlijk gepind, aan de kant waar hij hoort.

    Zonder de tweedeling glipt een nieuwe stille default erdoor: de hub start dan op een
    halve configuratie en wijst de spawner bijvoorbeeld naar een leeg image.
    """
    env = _full_env()
    del env[missing]

    if missing in OPTIONAL_ENV:
        _load_hub_config(env)
        return

    with pytest.raises(RuntimeError, match=missing):
        _load_hub_config(env)


def test_keycloak_is_the_gate_and_the_gate_names_a_role_the_project_itself_creates() -> None:
    """De poort van de POC is ``restrict-access`` op een realm-rol.

    Valt de restrictie weg, of wijst hij naar een rol die niemand aanmaakt, dan komt elke
    gebruiker van de realm binnen terwijl er niets zichtbaar verandert.
    """
    keycloak = next(
        service_entry_config(entry) for entry in _project()["services"] if service_entry_name(entry) == "keycloak"
    )

    restrict = keycloak["restrict-access"]
    assert restrict["enabled"] is True
    assert restrict["realm-role"] in {role["name"] for role in keycloak["realm-roles"]}


@pytest.mark.parametrize("dockerfile", ["jupyterhub-hub", "jupyterhub-user"])
def test_the_poc_images_pin_every_version(dockerfile: str) -> None:
    """Een POC die van run tot run een andere hub of editor krijgt, meet niets."""
    lines = (REPO_ROOT / "images" / dockerfile / "Dockerfile").read_text(encoding="utf-8").splitlines()

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("FROM "):
            image = stripped.split()[1]
            assert ":" in image, f"base-image zonder tag: {stripped}"
            assert not image.endswith(":latest"), f"base-image op latest: {stripped}"
        if stripped.startswith("ARG ") and "VERSION" in stripped:
            assert "=" in stripped, f"ARG zonder vaste waarde: {stripped}"
