"""De derde realmblauwdruk: een lokaal account geeft naast zijn wachtwoord een OTP.

Onder ``sso-support`` logt een lokaal account met alleen een wachtwoord in: de OTP-stap
staat op CONDITIONAL achter "Condition - user configured", dus wie geen OTP heeft ingesteld
krijgt er ook geen gevraagd. BIO2 8.05 vraagt sterkere authenticatie voor wie niet via de
federatie binnenkomt, en ``sso-support-otp`` is die keuze. In de master-realm, waar de
platformbeheerders en de beheeraccounts van de projectrealms staan, geldt het altijd.

Deze toetsen leggen vast WAT er aan de flow verandert en wat juist niet: ``sso-only`` en
``sso-support`` houden hun flow precies zoals hij was.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from keycloak.exceptions import KeycloakError
from opi.bootstrap.keycloak_setup import KeycloakSetup
from opi.connectors.keycloak import (
    CONFIGURE_TOTP,
    DEFAULT_BROWSER_FLOW,
    PLATFORM_IDP_ALIAS,
    REQUIRED_OTP_BROWSER_FLOW,
    KeycloakConnector,
    role_gate_flow_alias,
)
from opi.core.config import settings
from opi.handlers.keycloak_yaml_handler import KeycloakYamlHandler
from opi.manager.invite_manager import InviteManager
from opi.manager.keycloak_manager import BROWSER_FLOW_BY_TEMPLATE, KeycloakManager
from opi.services.catalog.keycloak.templates import (
    LOCAL_ACCOUNT_TEMPLATES,
    SSO_ONLY_TEMPLATE,
    SSO_SUPPORT_OTP_TEMPLATE,
    SSO_SUPPORT_TEMPLATE,
    allows_local_accounts,
)
from ruamel.yaml import YAML
from tests.test_keycloak_auto_link_flow import FakeAdmin

BLAUWDRUK_MAP = Path(__file__).parent.parent / "opi" / "configs" / "keycloak"

FORMS = f"{REQUIRED_OTP_BROWSER_FLOW} forms"
OTP = f"{REQUIRED_OTP_BROWSER_FLOW} otp"


def _connector() -> tuple[KeycloakConnector, FakeAdmin]:
    connector = KeycloakConnector.__new__(KeycloakConnector)
    admin = FakeAdmin()
    connector.admin = admin  # type: ignore[assignment]
    return connector, admin


def _namen(admin: FakeAdmin, alias: str) -> list[str | None]:
    return [e["displayName"] for e in admin.get_authentication_flow_executions(alias)]


def _stap(admin: FakeAdmin, alias: str, naam: str) -> dict[str, Any]:
    return next(e for e in admin.get_authentication_flow_executions(alias) if e["displayName"] == naam)


# --- de blauwdruk -------------------------------------------------------------------


def test_de_blauwdruk_bestaat_en_erft_sso_support() -> None:
    """Een kopie zou op de platform-client, de IdP en de mappers afdrijven."""
    eigen = YAML().load((BLAUWDRUK_MAP / f"{SSO_SUPPORT_OTP_TEMPLATE}.yaml").read_text())

    assert eigen["extends"] == SSO_SUPPORT_TEMPLATE


def test_de_blauwdruk_noemt_dezelfde_flow_als_de_connector_bouwt() -> None:
    """Het alias staat op twee plekken: de blauwdruk kiest hem, de connector bouwt hem."""
    eigen = YAML().load((BLAUWDRUK_MAP / f"{SSO_SUPPORT_OTP_TEMPLATE}.yaml").read_text())

    assert eigen["browserFlow"] == REQUIRED_OTP_BROWSER_FLOW
    assert eigen["authenticationFlows"][0]["alias"] == REQUIRED_OTP_BROWSER_FLOW
    assert eigen["authenticationFlows"][0]["requireOtp"] is True


def test_een_nieuw_lokaal_account_stelt_zijn_app_bij_de_eerste_login_in() -> None:
    eigen = YAML().load((BLAUWDRUK_MAP / f"{SSO_SUPPORT_OTP_TEMPLATE}.yaml").read_text())

    assert eigen["realms"][0]["requiredActionDefaults"] == {CONFIGURE_TOTP: True}


@pytest.mark.parametrize("naam", [SSO_ONLY_TEMPLATE, SSO_SUPPORT_TEMPLATE])
def test_de_andere_blauwdrukken_zetten_de_standaardactie_expliciet_uit(naam: str) -> None:
    """Zwijgen laat de stand van de voorganger staan.

    Gemeten op de sandbox: een realm die van ``sso-support-otp`` terug naar ``sso-support``
    ging, bleef elk NIEUW lokaal account om een authenticator-app vragen terwijl de inlogflow
    er niet meer om vroeg. De wissel hoort in beide richtingen te werken.
    """
    eigen = YAML().load((BLAUWDRUK_MAP / f"{naam}.yaml").read_text())

    assert eigen["realms"][0]["requiredActionDefaults"] == {CONFIGURE_TOTP: False}


@pytest.mark.asyncio
async def test_de_lezer_schrijft_beide_standen() -> None:
    keycloak = AsyncMock()
    handler = KeycloakYamlHandler(keycloak)

    for verwacht in (True, False):
        keycloak.reset_mock()
        await handler._apply_realm_self_service(
            "proj-odcn-production", {"requiredActionDefaults": {CONFIGURE_TOTP: verwacht}}
        )

        keycloak.set_required_action_default.assert_awaited_once_with(
            "proj-odcn-production", CONFIGURE_TOTP, default=verwacht
        )


def test_de_uitnodigingsclient_draagt_de_flow_van_het_realm() -> None:
    """Een clientoverride wint van de browserFlow van het realm.

    De uitnodigingsclient erft browserFlowOverride "browser" van sso-support, en dat gaf
    een publieke client waarop een lokaal account zonder OTP-credential alleen met zijn
    wachtwoord een realmsessie kreeg; de Cookie-stap liet die sessie daarna ook op de
    applicatieclient binnen. De override bestaat om de ROLPOORT te omzeilen, niet de
    authenticatie-eis van het realm.
    """
    handler = KeycloakYamlHandler(AsyncMock())
    config = handler._load_yaml_resolving_extends(BLAUWDRUK_MAP / f"{SSO_SUPPORT_OTP_TEMPLATE}.yaml", ())

    invite = next(c for c in config["clients"] if c["clientId"] == "{{ invite_client_id }}")
    assert invite["browserFlowOverride"] == REQUIRED_OTP_BROWSER_FLOW
    assert invite["publicClient"] is True

    basis = handler._load_yaml_resolving_extends(BLAUWDRUK_MAP / f"{SSO_SUPPORT_TEMPLATE}.yaml", ())
    basis_invite = next(c for c in basis["clients"] if c["clientId"] == "{{ invite_client_id }}")
    assert basis_invite["browserFlowOverride"] == DEFAULT_BROWSER_FLOW


@pytest.mark.asyncio
async def test_de_lezer_bouwt_de_otp_flow_via_de_connector_en_niet_als_platte_executie() -> None:
    """``requireOtp`` moet naar ``ensure_required_otp_browser_flow`` routeren en daar stoppen.

    De lezer kent alleen platte executions (``setAsBrowserFlow``); zonder de ``continue`` op
    de requireOtp-tak valt de nested-subflow-executie van de blauwdruk in die andere tak, die
    er geen ``executions`` voor heeft.
    """
    keycloak = AsyncMock()
    handler = KeycloakYamlHandler(keycloak)

    await handler.ensure_authentication_flows(
        BLAUWDRUK_MAP / f"{SSO_SUPPORT_OTP_TEMPLATE}.yaml",
        {"realm_name": "proj-odcn-production", "project_realm_name": "proj-odcn-production"},
    )

    keycloak.ensure_required_otp_browser_flow.assert_awaited_once_with(
        "proj-odcn-production", REQUIRED_OTP_BROWSER_FLOW
    )
    keycloak.create_authentication_flow.assert_not_called()
    keycloak.ensure_browser_flow.assert_awaited_once_with("proj-odcn-production", REQUIRED_OTP_BROWSER_FLOW)


def test_de_bestaande_blauwdrukken_vragen_geen_verplichte_otp() -> None:
    """De snapshot van de twee die er waren: hun inlogflow verandert niet."""
    for naam in (SSO_ONLY_TEMPLATE, SSO_SUPPORT_TEMPLATE):
        eigen = YAML().load((BLAUWDRUK_MAP / f"{naam}.yaml").read_text())
        flows = eigen.get("authenticationFlows") or []
        assert not any(flow.get("requireOtp") for flow in flows), naam
        assert not eigen["realms"][0]["requiredActionDefaults"][CONFIGURE_TOTP], naam

    assert YAML().load((BLAUWDRUK_MAP / "sso-support.yaml").read_text())["browserFlow"] == DEFAULT_BROWSER_FLOW


# --- de flow die OPI bouwt ----------------------------------------------------------


@pytest.mark.asyncio
async def test_de_otp_stap_is_verplicht_en_zonder_voorwaarde() -> None:
    """De kern: geen "Condition - user configured", dus ook wie geen OTP heeft krijgt de vraag."""
    connector, admin = _connector()

    await connector.ensure_required_otp_browser_flow("realm-a", REQUIRED_OTP_BROWSER_FLOW)

    assert _namen(admin, REQUIRED_OTP_BROWSER_FLOW) == ["auth-cookie", "identity-provider-redirector", FORMS]
    assert _namen(admin, FORMS) == ["auth-username-password-form", OTP]
    assert _stap(admin, FORMS, OTP)["requirement"] == "REQUIRED"
    assert _stap(admin, OTP, "auth-otp-form")["requirement"] == "REQUIRED"
    assert _stap(admin, OTP, "conditional-user-configured")["requirement"] == "DISABLED"


@pytest.mark.asyncio
async def test_een_sso_gebruiker_houdt_zijn_eigen_weg() -> None:
    """De redirector staat als alternatief naast de forms-subflow, dus SSO mist de OTP-stap."""
    connector, admin = _connector()

    await connector.ensure_required_otp_browser_flow("realm-a", REQUIRED_OTP_BROWSER_FLOW)

    assert _stap(admin, REQUIRED_OTP_BROWSER_FLOW, "identity-provider-redirector")["requirement"] == "ALTERNATIVE"
    assert _stap(admin, REQUIRED_OTP_BROWSER_FLOW, FORMS)["requirement"] == "ALTERNATIVE"


@pytest.mark.asyncio
async def test_de_flow_opnieuw_bouwen_verdubbelt_niets() -> None:
    connector, admin = _connector()

    await connector.ensure_required_otp_browser_flow("realm-a", REQUIRED_OTP_BROWSER_FLOW)
    await connector.ensure_required_otp_browser_flow("realm-a", REQUIRED_OTP_BROWSER_FLOW)

    assert _namen(admin, REQUIRED_OTP_BROWSER_FLOW) == ["auth-cookie", "identity-provider-redirector", FORMS]
    assert _namen(admin, OTP) == ["conditional-user-configured", "auth-otp-form"]


# --- de rolpoortflow volgt de template ----------------------------------------------


def _poort_aliassen() -> tuple[str, str, str]:
    poort = role_gate_flow_alias("proj-app")
    return poort, f"{poort}-forms", f"{poort}-otp"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("require_otp", "subflow", "voorwaarde"),
    [(True, "REQUIRED", "DISABLED"), (False, "CONDITIONAL", "REQUIRED")],
)
async def test_de_rolpoort_stelt_dezelfde_eis_als_het_realm(require_otp: bool, subflow: str, voorwaarde: str) -> None:
    """De poort REPLACES de browser flow van de realm op deze client.

    Zonder deze doorgifte zou een project met verplichte OTP en een authorization wall
    juist de lossere eis krijgen, precies op de client die beschermd moet zijn.
    """
    connector, admin = _connector()
    poort, forms, otp = _poort_aliassen()

    await connector.create_restricted_browser_flow_realm_role(
        realm_name="realm-a", flow_alias=poort, role_name="allowed-user", require_otp=require_otp
    )

    assert _stap(admin, forms, otp)["requirement"] == subflow
    assert _stap(admin, otp, "conditional-user-configured")["requirement"] == voorwaarde
    assert _stap(admin, otp, "auth-otp-form")["requirement"] == "REQUIRED"


@pytest.mark.asyncio
async def test_de_poort_komt_in_beide_richtingen_mee() -> None:
    """Een project dat de verplichte OTP weer afzet, hoort hem ook op de poort te verliezen."""
    connector, admin = _connector()
    poort, forms, otp = _poort_aliassen()

    for require_otp in (True, False):
        await connector.create_restricted_browser_flow_realm_role(
            realm_name="realm-a", flow_alias=poort, role_name="allowed-user", require_otp=require_otp
        )

    assert _stap(admin, forms, otp)["requirement"] == "CONDITIONAL"
    assert _stap(admin, otp, "conditional-user-configured")["requirement"] == "REQUIRED"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("template", "verwacht"),
    [(SSO_ONLY_TEMPLATE, False), (SSO_SUPPORT_TEMPLATE, False), (SSO_SUPPORT_OTP_TEMPLATE, True)],
)
async def test_de_manager_geeft_de_otp_eis_van_de_template_door(template: str, verwacht: bool) -> None:
    keycloak = AsyncMock()
    keycloak.get_identity_providers.return_value = []

    await KeycloakManager(project_manager=AsyncMock())._apply_access_restriction(
        keycloak=keycloak,
        realm_name="realm-a",
        client_id="proj-app",
        restrict_access={"enabled": True, "realm_role": "allowed-user"},
        template=template,
    )

    assert keycloak.create_restricted_browser_flow_realm_role.await_args.kwargs["require_otp"] is verwacht


# --- de browser flow van de realm ---------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("template", "verwacht"),
    [
        (SSO_ONLY_TEMPLATE, "External IDP Redirector"),
        (SSO_SUPPORT_TEMPLATE, DEFAULT_BROWSER_FLOW),
        (SSO_SUPPORT_OTP_TEMPLATE, REQUIRED_OTP_BROWSER_FLOW),
    ],
)
async def test_de_realm_krijgt_de_browser_flow_van_zijn_template(template: str, verwacht: str) -> None:
    keycloak = AsyncMock()
    keycloak.ensure_configure_totp_required.return_value = []

    with (
        patch("opi.manager.keycloak_manager.create_keycloak_connector", return_value=keycloak),
        patch.object(KeycloakYamlHandler, "ensure_authentication_flows", new=AsyncMock()),
    ):
        await KeycloakManager(project_manager=AsyncMock())._ensure_realm_authentication_flow(
            realm_name="proj-odcn-production",
            keycloak_url="https://keycloak.example.nl",
            config={"template": template},
        )

    keycloak.ensure_browser_flow.assert_awaited_once_with("proj-odcn-production", verwacht)


def test_de_kaart_laat_de_bestaande_templates_waar_ze_waren() -> None:
    assert BROWSER_FLOW_BY_TEMPLATE.get(SSO_SUPPORT_TEMPLATE, DEFAULT_BROWSER_FLOW) == DEFAULT_BROWSER_FLOW
    assert BROWSER_FLOW_BY_TEMPLATE[SSO_ONLY_TEMPLATE] == "External IDP Redirector"


# --- overstappen: de required action op bestaande accounts --------------------------


class FakeUsersAdmin:
    """Het gebruikersdeel van de admin-API, genoeg voor de CONFIGURE_TOTP-walk."""

    def __init__(self, users: list[dict[str, Any]]) -> None:
        self.users = users
        self.social: dict[str, list[dict[str, Any]]] = {}
        self.credentials: dict[str, list[dict[str, Any]]] = {}
        self.required_actions: dict[str, dict[str, Any]] = {
            CONFIGURE_TOTP: {"alias": CONFIGURE_TOTP, "enabled": False, "defaultAction": False}
        }
        self.realms: list[str] = []

    def change_current_realm(self, realm_name: str) -> None:
        self.realms.append(realm_name)

    def get_users(self, query: dict[str, Any]) -> list[dict[str, Any]]:
        first = query["first"]
        return self.users[first : first + query["max"]]

    def get_user_social_logins(self, user_id: str) -> list[dict[str, Any]]:
        return self.social.get(user_id, [])

    def get_credentials(self, user_id: str) -> list[dict[str, Any]]:
        return self.credentials.get(user_id, [])

    def update_user(self, user_id: str, payload: dict[str, Any]) -> None:
        for user in self.users:
            if user["id"] == user_id:
                user.update(payload)

    def get_required_action_by_alias(self, alias: str) -> dict[str, Any] | None:
        return self.required_actions.get(alias)

    def update_required_action(self, alias: str, payload: dict[str, Any]) -> None:
        self.required_actions[alias] = payload


def _users_connector(users: list[dict[str, Any]]) -> tuple[KeycloakConnector, FakeUsersAdmin]:
    connector = KeycloakConnector.__new__(KeycloakConnector)
    admin = FakeUsersAdmin(users)
    connector.admin = admin  # type: ignore[assignment]
    return connector, admin


def _acties(admin: FakeUsersAdmin, username: str) -> list[str]:
    user = next(u for u in admin.users if u["username"] == username)
    return list(user.get("requiredActions") or [])


@pytest.mark.asyncio
async def test_alleen_het_lokale_account_krijgt_de_required_action() -> None:
    """Een SSO-gebruiker haalt zijn tweede factor bij SSO Rijk, niet hier."""
    connector, admin = _users_connector(
        [
            {"id": "u1", "username": "lokaal"},
            {"id": "u2", "username": "via-sso"},
            {"id": "u3", "username": "service-account-proj-app"},
        ]
    )
    admin.social["u2"] = [{"identityProvider": "rig-platform-oidc"}]

    gewijzigd = await connector.ensure_configure_totp_required("proj-odcn-production")

    assert gewijzigd == ["lokaal"]
    assert _acties(admin, "lokaal") == [CONFIGURE_TOTP]
    assert _acties(admin, "via-sso") == []
    assert _acties(admin, "service-account-proj-app") == []
    assert admin.required_actions[CONFIGURE_TOTP]["enabled"] is True, (
        "een requiredAction die geen gebruiker vraagt om hem in te stellen, doet niets"
    )


@pytest.mark.asyncio
async def test_wie_al_een_otp_heeft_wordt_niet_gevraagd_hem_opnieuw_in_te_stellen() -> None:
    connector, admin = _users_connector([{"id": "u1", "username": "heeft-otp"}])
    admin.credentials["u1"] = [{"type": "password"}, {"type": "otp"}]

    assert await connector.ensure_configure_totp_required("proj-odcn-production") == []
    assert _acties(admin, "heeft-otp") == []


@pytest.mark.asyncio
async def test_een_tweede_ronde_verandert_niets() -> None:
    connector, admin = _users_connector([{"id": "u1", "username": "lokaal", "requiredActions": ["VERIFY_EMAIL"]}])

    eerst = await connector.ensure_configure_totp_required("proj-odcn-production")
    daarna = await connector.ensure_configure_totp_required("proj-odcn-production")

    assert eerst == ["lokaal"]
    assert daarna == []
    assert _acties(admin, "lokaal") == ["VERIFY_EMAIL", CONFIGURE_TOTP]


@pytest.mark.asyncio
async def test_de_walk_haalt_elke_pagina_op() -> None:
    """Zonder paginering levert Keycloak stil de eerste honderd gebruikers."""
    connector, _ = _users_connector([{"id": f"u{i}", "username": f"lokaal-{i}"} for i in range(150)])

    assert len(await connector.ensure_configure_totp_required("proj-odcn-production")) == 150


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("template", "verwacht"),
    [(SSO_SUPPORT_TEMPLATE, False), (SSO_SUPPORT_OTP_TEMPLATE, True)],
)
async def test_alleen_een_wissel_naar_de_otp_template_vraagt_de_required_action(template: str, verwacht: bool) -> None:
    keycloak = AsyncMock()
    keycloak.ensure_configure_totp_required.return_value = []

    with (
        patch("opi.manager.keycloak_manager.create_keycloak_connector", return_value=keycloak),
        patch.object(KeycloakYamlHandler, "ensure_authentication_flows", new=AsyncMock()),
    ):
        await KeycloakManager(project_manager=AsyncMock())._ensure_realm_authentication_flow(
            realm_name="proj-odcn-production",
            keycloak_url="https://keycloak.example.nl",
            config={"template": template},
        )

    assert (keycloak.ensure_configure_totp_required.await_count == 1) is verwacht


# --- de master-realm ----------------------------------------------------------------


def _setup(keycloak: AsyncMock) -> KeycloakSetup:
    setup = KeycloakSetup()
    setup.keycloak = keycloak
    return setup


@pytest.mark.asyncio
async def test_de_master_realm_krijgt_de_verplichte_otp_flow() -> None:
    keycloak = AsyncMock()
    keycloak.ensure_configure_totp_required.return_value = ["beheerder"]

    with patch("opi.bootstrap.keycloak_setup.settings.KEYCLOAK_ENFORCE_MASTER_OTP", True):
        await _setup(keycloak).ensure_master_otp()

    keycloak.ensure_required_otp_browser_flow.assert_awaited_once_with("master", REQUIRED_OTP_BROWSER_FLOW)
    keycloak.ensure_browser_flow.assert_awaited_once_with("master", REQUIRED_OTP_BROWSER_FLOW)
    keycloak.ensure_configure_totp_required.assert_awaited_once_with(
        "master", skip_usernames=frozenset({settings.KEYCLOAK_ADMIN_USERNAME})
    )


@pytest.mark.asyncio
async def test_het_account_waarmee_opi_inlogt_houdt_zijn_password_grant() -> None:
    """Een required action geeft "Account is not fully set up" op de password grant.

    Zonder KEYCLOAK_ADMIN_CLIENT_SECRET is dat de grant waarop create_keycloak_connector
    terugvalt, dus daar kan OPI na deze stap niet meer bij Keycloak. De flow van de realm
    vraagt dat account bij een consolelogin alsnog een OTP, en admin-otp is het menselijke
    account met factor.
    """
    connector, admin = _users_connector(
        [{"id": "u1", "username": settings.KEYCLOAK_ADMIN_USERNAME}, {"id": "u2", "username": "beheerder"}]
    )

    gewijzigd = await connector.ensure_configure_totp_required(
        "master", skip_usernames=frozenset({settings.KEYCLOAK_ADMIN_USERNAME})
    )

    assert gewijzigd == ["beheerder"]
    assert _acties(admin, settings.KEYCLOAK_ADMIN_USERNAME) == []


@pytest.mark.asyncio
async def test_de_instelling_uit_laat_de_master_realm_ongemoeid() -> None:
    """De weg terug bij een uitrolprobleem op de beheerconsole, zonder codewijziging."""
    keycloak = AsyncMock()

    with patch("opi.bootstrap.keycloak_setup.settings.KEYCLOAK_ENFORCE_MASTER_OTP", False):
        await _setup(keycloak).ensure_master_otp()

    keycloak.ensure_required_otp_browser_flow.assert_not_awaited()
    keycloak.ensure_browser_flow.assert_not_awaited()


@pytest.mark.asyncio
async def test_een_mislukte_otp_stap_laat_de_bootstrap_doorgaan() -> None:
    """Een master-realm zonder tweede factor is beter dan een OPI die niet start."""
    keycloak = AsyncMock()
    keycloak.ensure_required_otp_browser_flow.side_effect = KeycloakError("boem")

    with patch("opi.bootstrap.keycloak_setup.settings.KEYCLOAK_ENFORCE_MASTER_OTP", True):
        await _setup(keycloak).ensure_master_otp()

    keycloak.ensure_browser_flow.assert_not_awaited()


@pytest.mark.asyncio
async def test_de_bootstrap_zet_de_master_otp_na_het_harden() -> None:
    """De stap hoort in setup_all te staan, anders draait hij nooit."""
    setup = KeycloakSetup()
    volgorde: list[str] = []

    with (
        patch.object(KeycloakSetup, "ensure_master_admin_service_account", new=AsyncMock()),
        patch.object(KeycloakSetup, "ensure_otp_master_admin", new=AsyncMock()),
        patch.object(
            KeycloakSetup,
            "ensure_master_realm_hardening",
            new=AsyncMock(side_effect=lambda: volgorde.append("hardening")),
        ),
        patch.object(
            KeycloakSetup, "ensure_master_otp", new=AsyncMock(side_effect=lambda: volgorde.append("master-otp"))
        ),
        patch.object(KeycloakSetup, "setup_operations_realm", new=AsyncMock()),
        patch.object(KeycloakSetup, "setup_operations_client", new=AsyncMock(return_value=True)),
        patch("opi.bootstrap.keycloak_setup.create_keycloak_connector", new=AsyncMock()),
        patch("opi.bootstrap.keycloak_setup.KubectlConnector", MagicMock()),
        patch.object(KeycloakYamlHandler, "execute_config", new=AsyncMock()),
    ):
        assert await setup.setup_all() is True

    assert volgorde == ["hardening", "master-otp"]


# --- de regel over lokale accounts staat op één plek ---------------------------------


def test_de_blauwdrukken_met_een_lokaal_account() -> None:
    assert allows_local_accounts(SSO_SUPPORT_TEMPLATE)
    assert allows_local_accounts(SSO_SUPPORT_OTP_TEMPLATE)
    assert not allows_local_accounts(SSO_ONLY_TEMPLATE)
    assert not allows_local_accounts(None), "een leeg template is sso-only"


def test_de_uitnodiging_biedt_een_lokaal_account_onder_de_nieuwe_template() -> None:
    from opi.forms.visualizers.providers import InviteAuthMethodOptionsProvider

    for template in LOCAL_ACCOUNT_TEMPLATES:
        provider = InviteAuthMethodOptionsProvider(
            {"services": [{"name": "keycloak", "config": {"template": template}}]}
        )
        assert [o["value"] for o in provider.get_options()] == ["sso", "local"], template


def test_het_keuzeveld_toont_de_derde_template() -> None:
    from opi.forms.visualizers.providers import KeycloakTemplateOptionsProvider

    opties = {o["value"]: o for o in KeycloakTemplateOptionsProvider().get_options()}

    assert list(opties) == [SSO_ONLY_TEMPLATE, SSO_SUPPORT_TEMPLATE, SSO_SUPPORT_OTP_TEMPLATE]
    assert "OTP" in opties[SSO_SUPPORT_OTP_TEMPLATE]["label"]
    assert "authenticator-app" in opties[SSO_SUPPORT_OTP_TEMPLATE]["description"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("browser_flow", "verwacht_lokaal"),
    [
        (DEFAULT_BROWSER_FLOW, True),
        (REQUIRED_OTP_BROWSER_FLOW, True),
        ("External IDP Redirector", False),
    ],
)
async def test_de_uitnodigingspagina_ziet_een_lokaal_account_achter_de_otp_flow(
    browser_flow: str, verwacht_lokaal: bool
) -> None:
    """De lezer van de flownaam moet de OTP-flow meenemen, anders verdwijnt de knop.

    ``invite_routes`` leidt ``allow_local`` hieruit af, en de landingspagina laat de knop
    "Lokaal account" weg als die False is. Een toets op de naam ``browser`` alleen liet de
    nieuwe blauwdruk dus zonder uitnodiging voor een lokaal account.
    """
    keycloak = MagicMock()
    keycloak.get_identity_providers = AsyncMock(return_value=[{"alias": PLATFORM_IDP_ALIAS, "enabled": True}])
    keycloak.get_realm = AsyncMock(return_value={"browserFlow": browser_flow})

    with patch("opi.manager.invite_manager.create_keycloak_connector", new=AsyncMock(return_value=keycloak)):
        methoden = await InviteManager().get_realm_auth_methods("prj-odcn-production")

    assert methoden["sso"] is True
    assert methoden["local"] is verwacht_lokaal
