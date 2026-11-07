"""Gedeelde hulp voor de toetsen die tegen een echte Keycloak meten (de groep ``keycloak_live``).

`task test-keycloak` start de gepinde image met de startargumenten van de basisdeployment en
zet de adressen in de omgeving; `task test-keycloak-image` doet hetzelfde met een zelf
gebouwde image. Zonder die omgeving slaan de toetsen die deze hulp gebruiken zichzelf over,
zoals de andere integratietoetsen dat doen.

Hier staat wat meer dan een module nodig heeft, met opzet: de scenariotoetsen en de
admin-API-toetsen praten op dezelfde manier met dezelfde Keycloak, en een tweede exemplaar
van ``AdminApi`` of van de browserhulp zou meteen afdrijven.
"""

from __future__ import annotations

import html
import re
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

import httpx
from tests.integration.live import KEYCLOAK

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

TIMEOUT = 30.0

#: Het service-accountsecret waarmee de bootstrap de master-client aanmaakt. Wegwerp: het
#: bereikt alleen de Keycloak waar deze groep tegen meet.
SERVICE_ACCOUNT_SECRET = "rc413-throwaway-service-account-secret"

#: Wachtwoord van de gebruikers die deze toetsen aanmaken. Lang genoeg voor het
#: wachtwoordbeleid van master (length(14)); plaatshoudervorm zodat de secret-scan hem als
#: zodanig herkent.
USER_PASSWORD = "example-Wachtwoord-1234"

#: Master-tokens leven standaard 60 seconden, dus het terugleeskanaal haalt er ruim daarvoor
#: een nieuwe.
TOKEN_MAX_AGE = 30.0


#: De omgeving lezen en de localhost-grendel komen uit de gedeelde hulp: elke live groep
#: doet dat hetzelfde, en een tweede exemplaar zou hier afdrijven.
omgeving = KEYCLOAK.omgeving
assert_local = KEYCLOAK.assert_local


def basis_url() -> str:
    return omgeving("KEYCLOAK_IMAGE_TEST_URL").rstrip("/")


def admin_token(url: str) -> str:
    antwoord = httpx.post(
        f"{url}/realms/master/protocol/openid-connect/token",
        data={
            "grant_type": "password",
            "client_id": "admin-cli",
            "username": omgeving("KEYCLOAK_IMAGE_TEST_ADMIN"),
            "password": omgeving("KEYCLOAK_IMAGE_TEST_ADMIN_PASSWORD"),
        },
        timeout=TIMEOUT,
    )
    assert antwoord.status_code == 200, antwoord.text
    return antwoord.json()["access_token"]


@contextmanager
def tijdelijke_realm(url: str, token: str, naam: str, **velden: Any) -> Iterator[dict[str, str]]:
    """Maakt een realm aan en haalt hem daarna weg, ook als de toets faalt.

    Levert de autorisatieheader op, want elke aanroep binnen het blok heeft hem nodig.
    """
    headers = {"Authorization": f"Bearer {token}"}
    realms = f"{url}/admin/realms"
    httpx.delete(f"{realms}/{naam}", headers=headers, timeout=TIMEOUT)
    gemaakt = httpx.post(realms, headers=headers, json={"realm": naam, "enabled": True, **velden}, timeout=TIMEOUT)
    assert gemaakt.status_code == 201, gemaakt.text
    try:
        yield headers
    finally:
        httpx.delete(f"{realms}/{naam}", headers=headers, timeout=TIMEOUT)


class AdminApi:
    """Read-back channel over the admin API, independent of the connector under test."""

    def __init__(self, url: str) -> None:
        self._url = url
        self._token = ""
        self._minted = 0.0

    @property
    def headers(self) -> dict[str, str]:
        if time.monotonic() - self._minted > TOKEN_MAX_AGE:
            self._token = admin_token(self._url)
            self._minted = time.monotonic()
        return {"Authorization": f"Bearer {self._token}"}

    def get(self, path: str, **params: Any) -> Any:
        answer = httpx.get(f"{self._url}{path}", headers=self.headers, params=params, timeout=TIMEOUT)
        assert answer.status_code == 200, f"GET {path}: {answer.status_code} {answer.text}"
        return answer.json()

    def put(self, path: str, payload: dict[str, Any]) -> None:
        answer = httpx.put(f"{self._url}{path}", headers=self.headers, json=payload, timeout=TIMEOUT)
        assert answer.status_code in (200, 204), f"PUT {path}: {answer.status_code} {answer.text}"

    def status(self, path: str) -> int:
        return httpx.get(f"{self._url}{path}", headers=self.headers, timeout=TIMEOUT).status_code

    def delete(self, path: str) -> int:
        return httpx.delete(f"{self._url}{path}", headers=self.headers, timeout=TIMEOUT).status_code

    def realm(self, realm_name: str) -> dict[str, Any]:
        return self.get(f"/admin/realms/{realm_name}")

    def clients(self, realm_name: str) -> list[dict[str, Any]]:
        return self.get(f"/admin/realms/{realm_name}/clients")

    def client(self, realm_name: str, client_id: str) -> dict[str, Any]:
        found = [c for c in self.clients(realm_name) if c["clientId"] == client_id]
        assert found, f"Client '{client_id}' not found in realm '{realm_name}'"
        return found[0]

    def client_or_none(self, realm_name: str, client_id: str) -> dict[str, Any] | None:
        return next((c for c in self.clients(realm_name) if c["clientId"] == client_id), None)

    def identity_providers(self, realm_name: str) -> list[dict[str, Any]]:
        return self.get(f"/admin/realms/{realm_name}/identity-provider/instances")

    def idp(self, realm_name: str, alias: str) -> dict[str, Any]:
        return self.get(f"/admin/realms/{realm_name}/identity-provider/instances/{alias}")

    def idp_mappers(self, realm_name: str, alias: str) -> list[dict[str, Any]]:
        return self.get(f"/admin/realms/{realm_name}/identity-provider/instances/{alias}/mappers")

    def required_action(self, realm_name: str, alias: str) -> dict[str, Any]:
        actions = self.get(f"/admin/realms/{realm_name}/authentication/required-actions")
        found = [a for a in actions if a["alias"] == alias]
        assert found, f"Required action '{alias}' not registered in realm '{realm_name}'"
        return found[0]

    def flows(self, realm_name: str) -> list[dict[str, Any]]:
        return self.get(f"/admin/realms/{realm_name}/authentication/flows")

    def executions(self, realm_name: str, flow_alias: str) -> list[dict[str, Any]]:
        quoted = flow_alias.replace(" ", "%20")
        return self.get(f"/admin/realms/{realm_name}/authentication/flows/{quoted}/executions")

    def authenticator_config(self, realm_name: str, config_id: str) -> dict[str, Any]:
        return self.get(f"/admin/realms/{realm_name}/authentication/config/{config_id}")

    def user(self, realm_name: str, username: str) -> dict[str, Any]:
        users = self.get(f"/admin/realms/{realm_name}/users", username=username, exact="true")
        assert users, f"User '{username}' not found in realm '{realm_name}'"
        return users[0]

    def credential_types(self, realm_name: str, user_id: str) -> set[str]:
        credentials = self.get(f"/admin/realms/{realm_name}/users/{user_id}/credentials")
        return {c["type"] for c in credentials}

    def realm_roles_of_user(self, realm_name: str, user_id: str) -> set[str]:
        roles = self.get(f"/admin/realms/{realm_name}/users/{user_id}/role-mappings/realm")
        return {r["name"] for r in roles}

    def client_roles_of_user(self, realm_name: str, user_id: str, client_uuid: str) -> set[str]:
        roles = self.get(f"/admin/realms/{realm_name}/users/{user_id}/role-mappings/clients/{client_uuid}")
        return {r["name"] for r in roles}

    def default_role_composites(self, realm_name: str) -> list[dict[str, Any]]:
        return self.get(f"/admin/realms/{realm_name}/roles/default-roles-{realm_name}/composites")

    def client_scopes(self, realm_name: str) -> list[dict[str, Any]]:
        return self.get(f"/admin/realms/{realm_name}/client-scopes")

    def default_realm_scopes(self, realm_name: str) -> set[str]:
        scopes = self.get(f"/admin/realms/{realm_name}/default-default-client-scopes")
        return {s["name"] for s in scopes}

    def default_client_scopes(self, realm_name: str, client_uuid: str) -> set[str]:
        scopes = self.get(f"/admin/realms/{realm_name}/clients/{client_uuid}/default-client-scopes")
        return {s["name"] for s in scopes}

    def scope_mappers(self, realm_name: str, scope_id: str) -> list[dict[str, Any]]:
        return self.get(f"/admin/realms/{realm_name}/client-scopes/{scope_id}/protocol-mappers/models")

    def user_profile(self, realm_name: str) -> dict[str, Any]:
        return self.get(f"/admin/realms/{realm_name}/users/profile")

    def drop_realm(self, realm_name: str) -> None:
        self.delete(f"/admin/realms/{realm_name}")


def execution(executions: list[dict[str, Any]], provider_id: str) -> dict[str, Any]:
    found = [e for e in executions if e.get("providerId") == provider_id]
    assert found, f"No '{provider_id}' execution; present: {[e.get('displayName') for e in executions]}"
    return found[0]


def subflow(executions: list[dict[str, Any]], display_name: str) -> dict[str, Any]:
    found = [e for e in executions if e.get("displayName") == display_name and e.get("authenticationFlow")]
    assert found, f"No subflow '{display_name}'; present: {[e.get('displayName') for e in executions]}"
    return found[0]


def browser_client(url: str | None = None) -> httpx.Client:
    """Een httpx-client die zich bij het inloggen als een browser gedraagt.

    Keycloak 26.8 zet zijn sessiecookies `Secure` en rendert links relatief aan de host. Een
    browser ziet http://localhost als een veilige context en stuurt ze mee; httpx houdt ze in
    de jar maar stuurt ze niet over http, en dan eindigt elke formulierpost in
    "cookie not found" (400). De hook stuurt ze mee, zoals een browser doet.
    """
    client = httpx.Client(base_url=url or basis_url(), timeout=TIMEOUT, follow_redirects=True)

    def send_secure_cookies(request: httpx.Request) -> None:
        cookies = [f"{c.name}={c.value}" for c in client.cookies.jar if request.url.path.startswith(c.path)]
        if cookies:
            request.headers["Cookie"] = "; ".join(cookies)

    client.event_hooks["request"] = [send_secure_cookies]
    return client


def form_action(page: str, form_id: str) -> str:
    """De action-URL van het formulier met dit id, in de volgorde waarin het er staat."""
    tag = re.search(rf"<form[^>]*\bid=\"{re.escape(form_id)}\"[^>]*>", page)
    assert tag is not None, f"geen formulier met id {form_id} in de pagina:\n{page[:3000]}"
    action = re.search(r'\baction="([^"]+)"', tag.group(0))
    assert action is not None, tag.group(0)
    return html.unescape(action.group(1))


#: De authenticators waarvan een execution zonder config stil elke login in het realm breekt,
#: met de configsleutel die er dan hoort te staan.
CONFIGURED_AUTHENTICATORS = {
    "conditional-user-role": "condUserRole",
    "deny-access-authenticator": "denyErrorMessage",
    "require-client-role-authenticator": "roleName",
}


def assert_realm_consistent(
    api: AdminApi,
    realm_name: str,
    *,
    step: str,
    gated_clients: Mapping[str, str] | None = None,
    redirector_provider: str | None = None,
) -> None:
    """Eis na elke stap van een scenario dat het hele realm nog klopt.

    Dit is de toets die ook de fout vangt die we nog niet kennen: een execution zonder config
    is precies wat een stil geslikte 409 achterlaat, en dan faalt elke SSO-login die er langs
    komt. ``step`` komt in elke melding terecht, want een scenario doet tientallen stappen.

    Args:
        gated_clients: client-id naar de flow-alias die als ``browserFlowOverride`` op die
            client en op zijn ``-public`` moet staan.
        redirector_provider: de identity provider waar een poort naartoe hoort te sturen, of
            None voor een blauwdruk die het inlogscherm toont (``sso-support``).
    """
    gated_clients = gated_clients or {}
    flows = api.flows(realm_name)
    flow_ids = {flow["id"] for flow in flows}
    flow_by_alias = {flow["alias"]: flow for flow in flows}

    config_ids_by_alias: dict[str, set[str]] = {}
    for flow in flows:
        if not flow.get("topLevel"):
            continue
        for item in api.executions(realm_name, flow["alias"]):
            provider_id = item.get("providerId")
            config_id = item.get("authenticationConfig")
            if config_id:
                stored = api.authenticator_config(realm_name, config_id)
                config_ids_by_alias.setdefault(stored["alias"], set()).add(config_id)

            expected_key = CONFIGURED_AUTHENTICATORS.get(provider_id or "")
            if expected_key is None:
                continue
            where = f"{step}: execution '{provider_id}' in flow '{flow['alias']}' of realm '{realm_name}'"
            assert config_id, f"{where} has no authenticator config"
            assert api.authenticator_config(realm_name, config_id)["config"].get(expected_key), (
                f"{where} has a config without '{expected_key}'"
            )

    duplicates = {alias: sorted(ids) for alias, ids in config_ids_by_alias.items() if len(ids) > 1}
    assert not duplicates, f"{step}: authenticator config aliases used more than once in '{realm_name}': {duplicates}"

    for client in api.clients(realm_name):
        bound = (client.get("authenticationFlowBindingOverrides") or {}).get("browser")
        assert not bound or bound in flow_ids, (
            f"{step}: client '{client['clientId']}' overrides the browser flow with '{bound}', "
            f"which is not a flow in realm '{realm_name}'"
        )

    for client_id, flow_alias in gated_clients.items():
        assert flow_alias in flow_by_alias, f"{step}: gate flow '{flow_alias}' is missing from realm '{realm_name}'"
        expected_id = flow_by_alias[flow_alias]["id"]
        # Ook de publieke client: zonder override haalt dezelfde gebruiker daar zonder rol
        # een token voor dezelfde applicatie.
        for name in (client_id, f"{client_id}-public"):
            client = api.client(realm_name, name)
            bound = (client.get("authenticationFlowBindingOverrides") or {}).get("browser")
            assert bound == expected_id, f"{step}: client '{name}' is not gated by '{flow_alias}' (bound: {bound})"

        gate = api.executions(realm_name, flow_alias)
        redirector = execution(gate, "identity-provider-redirector")
        if redirector_provider is None:
            continue
        config_id = redirector.get("authenticationConfig")
        assert config_id, f"{step}: the redirector in gate '{flow_alias}' has no config, so it sends nobody anywhere"
        assert api.authenticator_config(realm_name, config_id)["config"].get("defaultProvider") == redirector_provider

    for idp in api.identity_providers(realm_name):
        alias = idp.get("postBrokerLoginFlowAlias")
        if not alias:
            continue
        assert alias in flow_by_alias, (
            f"{step}: IdP '{idp['alias']}' names post-broker flow '{alias}', which realm '{realm_name}' does not have"
        )
        authenticator = execution(api.executions(realm_name, alias), "require-client-role-authenticator")
        assert authenticator.get("authenticationConfig"), (
            f"{step}: the role check in post-broker flow '{alias}' has no config, "
            f"so every SSO login through IdP '{idp['alias']}' fails"
        )
