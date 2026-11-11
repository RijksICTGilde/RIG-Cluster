"""ZAD's Keycloak actions over SEVERAL deployments in ONE realm (RC-433).

``test_keycloak_admin_api.py`` measures every connector method on its own, on a fresh realm.
The 409 of Keycloak 26.8.1 showed what that misses: the fault appeared only when a second
deployment of the same project reached the same realm. So this module drives
``KeycloakManager`` instead of the connector, through the sequence production runs, and
checks the whole realm after every step with ``assert_realm_consistent``.

What is faked: nothing in Keycloak. ``FakeProjectManager`` is the slice of ``ProjectManager``
that ``KeycloakManager`` uses, over a project file in memory, because a real one needs git
repositories and a cluster. Requires ``KEYCLOAK_IMAGE_TEST_URL`` (set by `task
test-keycloak`) and skips without it.
"""

from __future__ import annotations

import base64
import hashlib
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import pyrage
import pytest
from opi.connectors.keycloak import PLATFORM_IDP_ALIAS, role_gate_flow_alias
from opi.core.cluster_config import CLUSTER_CONFIG
from opi.core.config import settings
from opi.handlers.project_file_handler import ProjectFileHandler
from opi.manager.delete_project_manager import DeleteProjectManager
from opi.manager.keycloak_manager import KeycloakManager
from opi.services.catalog.keycloak.templates import (
    SSO_ONLY_TEMPLATE,
    SSO_SUPPORT_OTP_TEMPLATE,
    SSO_SUPPORT_TEMPLATE,
)
from opi.services.project import Project
from opi.utils.naming import generate_project_admin_username, generate_project_realm_name
from opi.utils.totp import totp_now
from tests.integration.keycloak_image import (
    USER_PASSWORD,
    AdminApi,
    assert_realm_consistent,
    browser_client,
    execution,
    form_action,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import Protocol

    import httpx
    from opi.connectors.keycloak import KeycloakConnector
    from opi.utils.secrets import BaseSecret

    class HarnessFactory(Protocol):
        def __call__(
            self,
            *,
            template: str,
            deployments: list[str],
            restrict: str | None = None,
            with_browser_redirect: bool = False,
        ) -> Harness: ...


pytestmark = [pytest.mark.requires_infra, pytest.mark.keycloak_live]

#: Prefix of every realm this module creates, so leftovers are recognisable.
REALM_PREFIX = "scen-"

#: The cluster these scenarios run on. ``local`` is the only entry whose Keycloak carries
#: ``support_http``, which the browser logins below need.
CLUSTER = "local"

#: The role a restricted deployment demands, as a project file would name it.
ROLE = "allowed-user"

#: The raw TOTP seed of the users in the OTP scenario; Keycloak uses the string's bytes as
#: the HMAC key, see opi/utils/totp.py. Throwaway: only reaches the realms of this module.
OTP_SEED = "example-rc433-otp-seed"

#: The PKCE verifier of the logins below; the code is never exchanged, so one fixed value
#: is enough. Length and alphabet per RFC 7636.
PKCE_VERIFIER = "rc433scenarioverifier0123456789abcdefghijkl"

#: The templates with a local password; only these can be measured through the login form.
LOCAL_PASSWORD_TEMPLATES = (SSO_SUPPORT_TEMPLATE, SSO_SUPPORT_OTP_TEMPLATE)


class FakeProjectManager:
    """The slice of ``ProjectManager`` that ``KeycloakManager`` uses, over a file in memory.

    Everything Keycloak is real. This stands in for the git side only: reading the project
    file, writing it back, and the secrets map the deployment client lands in.
    """

    def __init__(self, project_data: dict[str, Any], keycloak_url: str) -> None:
        self.project_data = project_data
        self.keycloak_url = keycloak_url
        self.commits: list[str] = []
        self._secrets: dict[str, dict[str, BaseSecret]] = {}
        self._project_file_handler = ProjectFileHandler()

    async def get_name(self) -> str:
        return str(self.project_data["name"])

    async def get_contents(self, *, record_base: bool = True) -> dict[str, Any]:
        return self.project_data

    async def save_and_commit_project(
        self, project_data: dict[str, Any], message: str, enforce_validation: bool = True
    ) -> None:
        self.project_data = project_data
        self.commits.append(message)

    async def _get_project_keycloak_config_for_cluster(self, cluster: str) -> dict[str, Any] | None:
        realms = Project(self.project_data).get("services/keycloak/config/realms") or []
        expected = generate_project_realm_name(await self.get_name(), cluster)
        return next((entry for entry in realms if entry.get("realm") == expected), None)

    def _get_keycloak_url_for_cluster(self, cluster: str) -> str:
        return self.keycloak_url

    def _add_secret_to_create(self, deployment_name: str, secret_type: str, secret_data: BaseSecret) -> None:
        self._secrets.setdefault(deployment_name, {})[secret_type] = secret_data

    def _get_secret_from_map(
        self, deployment_name: str, secret_type: str, secret_class: type | None = None
    ) -> Any | None:
        return self._secrets.get(deployment_name, {}).get(secret_type)

    def drop_secrets(self, deployment_name: str) -> None:
        """Forget a deployment's client credentials, as removing it from the file does."""
        self._secrets.pop(deployment_name, None)

    def get_progress_manager(self) -> None:
        return None


def build_project_file(
    *,
    project: str,
    template: str,
    public_key: str,
    deployments: list[str],
    restrict: str | None = None,
    extra_redirect_uri: str | None = None,
) -> dict[str, Any]:
    """A project file in the shape the wizard writes, with one component per deployment.

    Args:
        restrict: ``realm`` for a realm role, ``client`` for a client role, None for no
            ``restrict-access`` at all.
    """
    config: dict[str, Any] = {"template": template}
    if restrict == "realm":
        config["restrict-access"] = {"enabled": True, "realm-role": ROLE}
        config["realm-roles"] = [{"name": ROLE, "description": "Access to the deployments of this project"}]
    elif restrict == "client":
        config["restrict-access"] = {"enabled": True, "role": ROLE}
    if extra_redirect_uri:
        config["additional-redirect-uris"] = [extra_redirect_uri]

    return {
        "name": project,
        "display-name": f"Scenario {project}",
        "clusters": [CLUSTER],
        "services": [{"name": "keycloak", "config": config}],
        "components": [{"name": "app", "type": "single", "ports": {"inbound": [8080]}, "services": ["keycloak"]}],
        "deployments": [
            {
                "name": name,
                "cluster": CLUSTER,
                "namespace": project,
                "subdomain": name,
                "components": [{"reference": "app"}],
            }
            for name in deployments
        ],
        "config": {"age-public-key": public_key},
    }


def deployment_entry(project_data: dict[str, Any], name: str) -> dict[str, Any]:
    found = next((d for d in project_data["deployments"] if d["name"] == name), None)
    assert found is not None, f"No deployment '{name}' in the project file"
    return found


class Harness:
    """One project file plus the realm it provisions, and the steps you can run on it."""

    def __init__(self, project: str, project_manager: FakeProjectManager, api: AdminApi) -> None:
        self.project = project
        self.project_manager = project_manager
        self.api = api
        self.realm = generate_project_realm_name(project, CLUSTER)

    @property
    def project_data(self) -> dict[str, Any]:
        return self.project_manager.project_data

    def client_id(self, deployment: str) -> str:
        return f"{self.project}-{deployment}"

    def gated_clients(self, deployments: list[str]) -> dict[str, str]:
        return {self.client_id(name): role_gate_flow_alias(self.client_id(name)) for name in deployments}

    async def process(self, deployments: list[str]) -> None:
        """One OPI processing run: one manager, every deployment in order.

        A fresh ``KeycloakManager`` per run on purpose: the realm-wide work it skips for a
        second deployment is remembered on the instance (RC-418), so reusing one would hide
        the second deployment's realm work.
        """
        manager = KeycloakManager(self.project_manager)  # type: ignore[arg-type]
        for name in deployments:
            await manager.create_resources_for_deployment(self.project_data, deployment_entry(self.project_data, name))

    async def remove(self, deployment: str) -> dict[str, Any]:
        manager = KeycloakManager(self.project_manager)  # type: ignore[arg-type]
        result = await manager.delete_resources_for_deployment(
            self.project_data, deployment_entry(self.project_data, deployment)
        )
        self.project_data["deployments"] = [d for d in self.project_data["deployments"] if d["name"] != deployment]
        self.project_manager.drop_secrets(deployment)
        return result

    def check(self, step: str, deployments: list[str], *, redirector_provider: str | None = None) -> None:
        assert_realm_consistent(
            self.api,
            self.realm,
            step=step,
            gated_clients=self.gated_clients(deployments),
            redirector_provider=redirector_provider,
        )


# ==================== Fixtures ====================


@pytest.fixture(scope="module")
def platform_realm(connector: KeycloakConnector, api: AdminApi) -> Iterator[str]:
    """A throwaway stand-in for rig-platform: the realm the project realms federate with.

    Created with OPI's own ``create_realm`` rather than through the bootstrap blueprint: what
    the project templates need from it is that it exists and serves discovery.
    """
    name = f"{REALM_PREFIX}platform-{uuid4().hex[:8]}"
    try:
        yield name
    finally:
        api.drop_realm(name)


@pytest.fixture
async def scenario_settings(
    patched_settings: None, keycloak_url: str, platform_realm: str, connector: KeycloakConnector
) -> Iterator[None]:
    """Point the platform settings and the cluster's Keycloak URL at the throwaway server."""
    if not await connector.realm_exists(platform_realm):
        await connector.create_realm(realm_name=platform_realm, display_name="Scenario platform")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(settings, "KEYCLOAK_DEFAULT_REALM", platform_realm)
        patch.setattr(settings, "CLUSTER_MANAGER", CLUSTER)
        # On Keycloak's own host, so the invite client's redirect lands on a 404 THERE. A
        # real domain would drop httpx on a DNS error, and the invite client is the only way
        # to a realm session on the stock browser flow.
        patch.setattr(settings, "OWN_DOMAIN", urlparse(keycloak_url).netloc)
        patch.setitem(CLUSTER_CONFIG[CLUSTER], "keycloak_discovery_url", keycloak_url)
        yield


@pytest.fixture
def make_harness(scenario_settings: None, api: AdminApi, keycloak_url: str) -> Iterator[HarnessFactory]:
    """Build a project with its own realm, and clean both up afterwards, failure or not."""
    made: list[Harness] = []

    def build(
        *, template: str, deployments: list[str], restrict: str | None = None, with_browser_redirect: bool = False
    ) -> Harness:
        project = f"{REALM_PREFIX}{uuid4().hex[:8]}"
        identity = pyrage.x25519.Identity.generate()
        project_data = build_project_file(
            project=project,
            template=template,
            public_key=str(identity.to_public()),
            deployments=deployments,
            restrict=restrict,
            # A redirect URI on Keycloak itself, so a completed login lands on a 404 here
            # instead of on a ConnectError for a hostname that does not resolve.
            extra_redirect_uri=f"{keycloak_url}/rc433-done" if with_browser_redirect else None,
        )
        harness = Harness(project, FakeProjectManager(project_data, keycloak_url), api)
        made.append(harness)
        return harness

    try:
        yield build
    finally:
        for harness in made:
            api.drop_realm(harness.realm)
            admin = generate_project_admin_username(harness.project, CLUSTER)
            users = api.get("/admin/realms/master/users", username=admin, exact="true")
            for user in users:
                api.delete(f"/admin/realms/master/users/{user['id']}")


# ==================== Several deployments in one realm ====================


@pytest.mark.parametrize("template", [SSO_ONLY_TEMPLATE, SSO_SUPPORT_TEMPLATE, SSO_SUPPORT_OTP_TEMPLATE])
@pytest.mark.parametrize("restrict", ["realm", "client"])
async def test_the_deployments_of_a_project_share_a_realm(
    make_harness: HarnessFactory, template: str, restrict: str
) -> None:
    """The whole life of a restricted project in one realm: add, add, reprocess, remove.

    This is the sequence the 409 broke: the second deployment wanted the same config alias
    as the first, Keycloak 26 refused it, and the whole run failed. The invariant after each
    step is what also catches the next fault of this kind.
    """
    harness = make_harness(template=template, deployments=["one", "two", "three"], restrict=restrict)
    # sso-only gates redirect straight to the platform IdP; the other templates show the
    # login form, and their redirector stays deliberately unconfigured.
    redirector = PLATFORM_IDP_ALIAS if template == SSO_ONLY_TEMPLATE else None

    await harness.process(["one"])
    harness.check("first deployment", ["one"], redirector_provider=redirector)

    await harness.process(["two"])
    harness.check("second deployment in the same realm", ["one", "two"], redirector_provider=redirector)

    await harness.process(["one", "two"])
    harness.check("reprocess of both", ["one", "two"], redirector_provider=redirector)

    await harness.process(["three"])
    harness.check("a third deployment", ["one", "two", "three"], redirector_provider=redirector)

    await harness.remove("two")
    assert harness.api.client_or_none(harness.realm, harness.client_id("two")) is None
    harness.check("one deployment removed", ["one", "three"], redirector_provider=redirector)

    await harness.process(["one", "three"])
    harness.check("reprocess of what is left", ["one", "three"], redirector_provider=redirector)


async def test_restrict_access_switched_on_for_existing_deployments(make_harness: HarnessFactory) -> None:
    """The gates arrive on a realm that already has two unrestricted deployments."""
    harness = make_harness(template=SSO_SUPPORT_TEMPLATE, deployments=["one", "two"])

    await harness.process(["one", "two"])
    harness.check("two deployments without a gate", [])
    for name in ("one", "two"):
        client = harness.api.client(harness.realm, harness.client_id(name))
        assert not (client.get("authenticationFlowBindingOverrides") or {}).get("browser")

    keycloak_config = harness.project_data["services"][0]["config"]
    keycloak_config["restrict-access"] = {"enabled": True, "realm-role": ROLE}
    keycloak_config["realm-roles"] = [{"name": ROLE}]

    await harness.process(["one", "two"])
    harness.check("restrict-access switched on", ["one", "two"])


@pytest.mark.parametrize(
    ("first", "second"),
    [(SSO_SUPPORT_TEMPLATE, SSO_ONLY_TEMPLATE), (SSO_ONLY_TEMPLATE, SSO_SUPPORT_TEMPLATE)],
)
async def test_a_template_switch_on_an_existing_realm(make_harness: HarnessFactory, first: str, second: str) -> None:
    """Switching the blueprint has to carry the gates with it, in both directions.

    A gate replaces the realm's browser flow on its client, so the redirect of ``sso-only``
    only reaches a restricted deployment through the gate's own redirector.
    """
    harness = make_harness(template=first, deployments=["one", "two"], restrict="realm")

    await harness.process(["one", "two"])
    harness.check(
        f"two deployments on {first}",
        ["one", "two"],
        redirector_provider=PLATFORM_IDP_ALIAS if first == SSO_ONLY_TEMPLATE else None,
    )

    harness.project_data["services"][0]["config"]["template"] = second
    await harness.process(["one", "two"])
    harness.check(
        f"switched to {second}",
        ["one", "two"],
        redirector_provider=PLATFORM_IDP_ALIAS if second == SSO_ONLY_TEMPLATE else None,
    )
    assert harness.api.realm(harness.realm)["browserFlow"] == (
        "External IDP Redirector" if second == SSO_ONLY_TEMPLATE else "browser"
    )


async def test_the_platform_client_is_repaired_on_the_next_run(
    make_harness: HarnessFactory, platform_realm: str
) -> None:
    """The direct ``admin.update_client`` path in the manager, on a client that drifted.

    ``_ensure_idp_and_platform_client_configuration`` writes through the admin API rather
    than through a connector method, so nothing else measures it.
    """
    harness = make_harness(template=SSO_SUPPORT_TEMPLATE, deployments=["one"])
    await harness.process(["one"])

    platform_client_id = f"{harness.project}-{CLUSTER}-platform"
    client = harness.api.client(platform_realm, platform_client_id)
    expected_uris = client["redirectUris"]
    expected_attributes = client["attributes"]
    assert expected_uris, "The platform client was created without a redirect URI"

    harness.api.put(
        f"/admin/realms/{platform_realm}/clients/{client['id']}",
        {**client, "redirectUris": ["https://drifted.example.invalid/*"], "attributes": {}},
    )

    await harness.process(["one"])

    repaired = harness.api.client(platform_realm, platform_client_id)
    assert set(expected_uris) <= set(repaired["redirectUris"])
    for key, value in expected_attributes.items():
        assert repaired["attributes"].get(key) == value, key


async def test_deleting_the_project_removes_the_realm_and_its_admin(
    make_harness: HarnessFactory, platform_realm: str
) -> None:
    """The cleanup ``tests/test_delete_keycloak_cleanup.py`` measures with mocks, for real."""
    harness = make_harness(template=SSO_SUPPORT_TEMPLATE, deployments=["one"], restrict="realm")
    await harness.process(["one"])

    admin_username = generate_project_admin_username(harness.project, CLUSTER)
    assert harness.api.user("master", admin_username)
    platform_client_id = f"{harness.project}-{CLUSTER}-platform"
    assert harness.api.client_or_none(platform_realm, platform_client_id) is not None

    manager = object.__new__(DeleteProjectManager)
    manager.project_manager = harness.project_manager
    results: dict[str, Any] = {"operations": [], "errors": [], "success": True}
    kc_config = await harness.project_manager._get_project_keycloak_config_for_cluster(CLUSTER)
    assert kc_config is not None
    await manager._cleanup_project_keycloak_realm(
        project_name=harness.project,
        cluster=CLUSTER,
        kc_config=kc_config,
        deletion_results=results,
        only_if_present=True,
    )

    assert results["errors"] == []
    assert harness.api.status(f"/admin/realms/{harness.realm}") == 404
    assert harness.api.get("/admin/realms/master/users", username=admin_username, exact="true") == []
    assert harness.api.client_or_none(platform_realm, platform_client_id) is None
    assert Project(harness.project_manager.project_data).get("services/keycloak/config/realms") == []


# ==================== Behaviour: really logging in through the gate ====================


def authorize(http: httpx.Client, keycloak_url: str, realm: str, client_id: str, redirect_uri: str) -> httpx.Response:
    """Start an authorization code flow, as a browser hitting a protected page would.

    PKCE is always sent: the invite client demands S256 and refuses the request without it,
    and a client that does not demand it accepts it anyway.
    """
    challenge = base64.urlsafe_b64encode(hashlib.sha256(PKCE_VERIFIER.encode()).digest()).decode().rstrip("=")
    return http.get(
        f"{keycloak_url}/realms/{realm}/protocol/openid-connect/auth",
        params={
            "client_id": client_id,
            "response_type": "code",
            "scope": "openid",
            "redirect_uri": redirect_uri,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
    )


def fresh_otp(seed: str, username: str, used: set[tuple[str, str]]) -> str:
    """A code this user has not used yet; Keycloak refuses a replayed one.

    Waits for the next period only when the current code was already used, so a run with one
    OTP login never waits at all. A code from the NEXT window would also be accepted
    (Keycloak looks one period ahead), but then two logins inside a second share it again.
    """
    code, seconds_left = totp_now(seed)
    if (username, code) in used:
        time.sleep(seconds_left + 1)
        code, _ = totp_now(seed)
    used.add((username, code))
    return code


def gate_login(
    http: httpx.Client,
    keycloak_url: str,
    realm: str,
    client_id: str,
    username: str,
    *,
    redirect_uri: str,
    otp_seed: str | None = None,
    otp_used: set[tuple[str, str]] | None = None,
) -> httpx.Response:
    """Log in at a gated client the way a browser does, and return where the flow ends."""
    page = authorize(http, keycloak_url, realm, client_id, redirect_uri)
    assert page.status_code == 200, page.text[:2000]
    answer = http.post(
        form_action(page.text, "kc-form-login"),
        data={"username": username, "password": USER_PASSWORD, "credentialId": ""},
    )
    if otp_seed is None:
        return answer
    # The code is minted here and not by the caller: it is valid for one period, and the
    # password post above costs part of it.
    code = fresh_otp(otp_seed, username, otp_used if otp_used is not None else set())
    return http.post(form_action(answer.text, "kc-otp-login-form"), data={"otp": code})


def admitted(answer: httpx.Response) -> bool:
    """Whether the flow ended at the redirect URI with an authorization code.

    On the query parameter and not on the text ``code=``: a denied login ends on
    ``login-actions/authenticate?session_code=...``, and that contains it too.
    """
    return "code" in parse_qs(urlparse(str(answer.url)).query)


@pytest.mark.parametrize("template", LOCAL_PASSWORD_TEMPLATES)
async def test_only_a_role_holder_gets_through_the_gate(
    make_harness: HarnessFactory, connector: KeycloakConnector, keycloak_url: str, template: str
) -> None:
    """A config that reads correctly does not prove the gate works, so this logs in.

    Measured on the step the 409 broke: two deployments in one realm, each with its own
    gate. The third case is the one the gate was rebuilt for: a realm session of a user
    without the role, which the Cookie step used to let past (see ``_build_role_gate_flow``).
    """
    harness = make_harness(template=template, deployments=["one", "two"], restrict="realm", with_browser_redirect=True)
    await harness.process(["one", "two"])
    harness.check("two gated deployments", ["one", "two"])

    # sso-support-otp requires an OTP at every password login, so both users carry a seed
    # from the start: configuring one is a flow of its own and not what this measures.
    seed = OTP_SEED if template == SSO_SUPPORT_OTP_TEMPLATE else None
    holder = await connector.create_user(
        realm_name=harness.realm,
        username="holder",
        password=USER_PASSWORD,
        email="holder@rijksoverheid.nl",
        totp_secret=seed,
        skip_email_verification=True,
    )
    for username in ("outsider", "bystander"):
        await connector.create_user(
            realm_name=harness.realm,
            username=username,
            password=USER_PASSWORD,
            email=f"{username}@rijksoverheid.nl",
            totp_secret=seed,
            skip_email_verification=True,
        )
    await connector.assign_realm_roles_to_user(harness.realm, holder["id"], [ROLE])

    if seed:
        # sso-support-otp makes CONFIGURE_TOTP a default action, so a new account first has
        # to set up an authenticator. These two already carry the seed, which is what an
        # account that completed that step looks like.
        for username in ("holder", "outsider", "bystander"):
            user = harness.api.user(harness.realm, username)
            harness.api.put(f"/admin/realms/{harness.realm}/users/{user['id']}", {**user, "requiredActions": []})

    deployment_redirect = f"{keycloak_url}/rc433-done"
    used_codes: set[tuple[str, str]] = set()
    for deployment in ("one", "two"):
        with browser_client(keycloak_url) as http:
            answer = gate_login(
                http,
                keycloak_url,
                harness.realm,
                harness.client_id(deployment),
                "holder",
                redirect_uri=deployment_redirect,
                otp_seed=seed,
                otp_used=used_codes,
            )
            assert admitted(answer), f"The role holder was refused at '{deployment}': {answer.text[:1500]}"

        with browser_client(keycloak_url) as http:
            answer = gate_login(
                http,
                keycloak_url,
                harness.realm,
                harness.client_id(deployment),
                "outsider",
                redirect_uri=deployment_redirect,
                otp_seed=seed,
                otp_used=used_codes,
            )
            assert not admitted(answer), f"A user without the role got into '{deployment}'"

    # A session in the realm, made at the invite client, which runs the stock browser flow
    # on purpose so an invitation can be completed without a role. A THIRD user without the
    # role, and not the one refused above: the realm is bruteForceProtected, a refusal at the
    # gate counts as a login failure, and two in a row inside a second make Keycloak hold
    # that user off for a minute (quickLoginCheckMilliSeconds).
    with browser_client(keycloak_url) as http:
        answer = gate_login(
            http,
            keycloak_url,
            harness.realm,
            settings.INVITE_CLIENT_ID,
            "bystander",
            redirect_uri=f"{keycloak_url}/invite/rc433-done",
            otp_seed=seed,
            otp_used=used_codes,
        )
        assert admitted(answer), f"The invite client refused a user without the role: {answer.text[:1500]}"

        gated = http.get(
            f"{keycloak_url}/realms/{harness.realm}/protocol/openid-connect/auth",
            params={
                "client_id": harness.client_id("one"),
                "response_type": "code",
                "scope": "openid",
                "redirect_uri": f"{keycloak_url}/rc433-done",
            },
        )
        assert not admitted(gated), "An existing realm session walked past the role gate"


# ==================== The authenticators behind the gate ====================


async def test_both_gates_in_a_realm_keep_their_own_config(make_harness: HarnessFactory) -> None:
    """Named explicitly, because this is the shape of the production fault.

    The invariant above proves every config is there; this one proves the two gates do not
    share one, which is what the realm-wide alias used to force.
    """
    harness = make_harness(template=SSO_ONLY_TEMPLATE, deployments=["one", "two"], restrict="realm")
    await harness.process(["one", "two"])

    configs = []
    for deployment in ("one", "two"):
        gate = harness.api.executions(harness.realm, role_gate_flow_alias(harness.client_id(deployment)))
        for provider in ("conditional-user-role", "identity-provider-redirector"):
            config_id = execution(gate, provider)["authenticationConfig"]
            assert config_id
            configs.append(harness.api.authenticator_config(harness.realm, config_id)["alias"])

    assert len(set(configs)) == len(configs), f"Two gates share a config alias: {configs}"
