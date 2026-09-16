"""De browser flow die toegang beperkt tot houders van een rol.

DE BEVINDING (25 augustus 2026)

De rolcontrole hing in de forms-subflow van een kopie van de browser flow. Daar draait hij
alleen op het pad waar iemand zijn wachtwoord intikt. De Cookie-stap staat als alternatief
daarvóór, dus wie al een sessie in de realm had kwam er ongetoetst langs. En zo'n sessie is
makkelijk te krijgen: elke andere client in dezelfde realm gebruikt de standaard browser
flow, waaronder de ingebouwde account-console en de ``<client>-public`` die OPI zelf naast
elke deployment aanmaakt. Nagespeeld op Keycloak 25.0.6: een lokale gebruiker zonder de rol
logde in op de account-console en haalde daarna bij de beschermde client een
authorization code op, waarmee de authorization wall hem doorliet.

DE REPARATIE

De rolcontrole staat nu op hetzelfde niveau als het inloggen, niet erin. Dat kan alleen als
de authenticators samen in een REQUIRED omhulsel zitten: Keycloak negeert ALTERNATIVE-stappen
zodra er op datzelfde niveau iets REQUIRED staat, dus de controle rechtstreeks naast Cookie
en forms zetten weigert iedereen, ook rolhouders.

Deze tests leggen de vorm vast, want de vorm IS de beveiliging.
"""

from typing import Any
from unittest.mock import AsyncMock

import pytest
from opi.connectors.keycloak import KeycloakConnector, legacy_restricted_flow_alias, role_gate_flow_alias
from opi.manager.keycloak_manager import KeycloakManager
from tests.test_keycloak_auto_link_flow import FakeAdmin

FLOW = role_gate_flow_alias("proj-app")
AUTHENTICATE = f"{FLOW}-authenticate"
FORMS = f"{FLOW}-forms"
OTP = f"{FLOW}-otp"
DENY = f"{FLOW}-deny-no-role"


def _connector() -> tuple[KeycloakConnector, FakeAdmin]:
    connector = KeycloakConnector.__new__(KeycloakConnector)
    admin = FakeAdmin()
    connector.admin = admin  # type: ignore[assignment]
    return connector, admin


def _namen(admin: FakeAdmin, alias: str) -> list[str | None]:
    return [e["displayName"] for e in admin.get_authentication_flow_executions(alias)]


def _stap(admin: FakeAdmin, alias: str, naam: str) -> dict[str, Any]:
    return next(e for e in admin.get_authentication_flow_executions(alias) if e["displayName"] == naam)


async def _bouw(
    connector: KeycloakConnector, *, client_id: str | None = None, redirect_to_idp: str | None = None
) -> None:
    if client_id:
        await connector.create_restricted_browser_flow(
            realm_name="realm-a",
            flow_alias=FLOW,
            client_id=client_id,
            role_name="allowed-user",
            redirect_to_idp=redirect_to_idp,
        )
    else:
        await connector.create_restricted_browser_flow_realm_role(
            realm_name="realm-a", flow_alias=FLOW, role_name="allowed-user", redirect_to_idp=redirect_to_idp
        )


def _redirector_config(admin: FakeAdmin) -> dict[str, Any] | None:
    """De config van de identity-provider-redirector, of None als hij er geen heeft."""
    redirector = _stap(admin, AUTHENTICATE, "identity-provider-redirector")
    config_id = redirector.get("authenticationConfig")
    return admin.configs.get(config_id) if config_id else None


@pytest.mark.asyncio
async def test_de_rolcontrole_staat_naast_het_inloggen_en_niet_erin() -> None:
    """De kern van de reparatie: geen enkel inlogpad kan de controle overslaan."""
    connector, admin = _connector()

    await _bouw(connector)

    assert _namen(admin, FLOW) == [AUTHENTICATE, DENY], "de rolcontrole hoort op het TOPniveau te staan"
    assert DENY not in _namen(admin, FORMS), "in de forms-subflow slaat de Cookie-stap hem over"
    assert _stap(admin, FLOW, DENY)["requirement"] == "CONDITIONAL"


@pytest.mark.asyncio
async def test_de_authenticators_zitten_samen_in_een_required_omhulsel() -> None:
    """Zonder dat omhulsel schakelt Keycloak alle ALTERNATIVE-stappen uit en komt niemand binnen."""
    connector, admin = _connector()

    await _bouw(connector)

    assert _stap(admin, FLOW, AUTHENTICATE)["requirement"] == "REQUIRED"
    assert _namen(admin, AUTHENTICATE) == ["auth-cookie", "identity-provider-redirector", FORMS]
    for naam in ("auth-cookie", "identity-provider-redirector", FORMS):
        assert _stap(admin, AUTHENTICATE, naam)["requirement"] == "ALTERNATIVE"


@pytest.mark.asyncio
async def test_de_rolcontrole_komt_na_het_inloggen() -> None:
    """Andersom is er nog geen gebruiker om op te toetsen, en weigert hij iedereen."""
    connector, admin = _connector()

    await _bouw(connector)

    assert _stap(admin, FLOW, AUTHENTICATE)["priority"] < _stap(admin, FLOW, DENY)["priority"]


@pytest.mark.asyncio
async def test_de_tweede_factor_blijft_staan() -> None:
    """De realmbeheerders hebben TOTP; die stap mag niet sneuvelen bij het herbouwen."""
    connector, admin = _connector()

    await _bouw(connector)

    assert _namen(admin, FORMS) == ["auth-username-password-form", OTP]
    assert _stap(admin, FORMS, OTP)["requirement"] == "CONDITIONAL"
    assert _namen(admin, OTP) == ["conditional-user-configured", "auth-otp-form"]


@pytest.mark.asyncio
async def test_de_voorwaarde_toetst_de_rol_omgekeerd() -> None:
    """Weigeren gebeurt als iemand de rol NIET heeft, dus de voorwaarde staat op negate."""
    connector, admin = _connector()

    await _bouw(connector)

    condities = [c for c in admin.configs.values() if "condUserRole" in c.get("config", {})]
    assert len(condities) == 1
    assert condities[0]["config"] == {"condUserRole": "allowed-user", "negate": "true"}


@pytest.mark.asyncio
async def test_een_clientrol_draagt_de_client_in_de_voorwaarde() -> None:
    connector, admin = _connector()

    await _bouw(connector, client_id="proj-app")

    condities = [c for c in admin.configs.values() if "condUserRole" in c.get("config", {})]
    assert condities[0]["config"]["condUserRole"] == "proj-app.allowed-user"


@pytest.mark.asyncio
async def test_opnieuw_toepassen_verdubbelt_niets() -> None:
    """Elke reprocess bouwt deze flow opnieuw op."""
    connector, admin = _connector()

    await _bouw(connector)
    await _bouw(connector)

    assert _namen(admin, FLOW) == [AUTHENTICATE, DENY]
    assert _namen(admin, AUTHENTICATE) == ["auth-cookie", "identity-provider-redirector", FORMS]
    assert _namen(admin, FORMS) == ["auth-username-password-form", OTP]
    assert _namen(admin, DENY) == ["conditional-user-role", "deny-access-authenticator"]


def _connector_mock() -> AsyncMock:
    keycloak = AsyncMock()
    # Geen identity providers, dan stopt de post-broker-tak meteen; die staat hier niet terecht.
    keycloak.get_identity_providers.return_value = []
    return keycloak


@pytest.mark.asyncio
async def test_de_poort_hangt_ook_op_de_publieke_client() -> None:
    """``<client>-public`` hoort bij dezelfde applicatie en stond zonder rolcontrole."""
    keycloak = _connector_mock()

    await KeycloakManager(project_manager=AsyncMock())._apply_access_restriction(
        keycloak=keycloak,
        realm_name="realm-a",
        client_id="proj-app",
        restrict_access={"enabled": True, "realm_role": "allowed-user"},
    )

    gebonden = [c.kwargs["client_id"] for c in keycloak.set_client_authentication_flow_override.call_args_list]
    assert gebonden == ["proj-app", "proj-app-public"]
    for call in keycloak.set_client_authentication_flow_override.call_args_list:
        assert call.kwargs["browser_flow_alias"] == role_gate_flow_alias("proj-app")


@pytest.mark.asyncio
async def test_een_ontbrekende_publieke_client_breekt_de_uitrol_niet() -> None:
    """Deployments van voor de publieke client hebben er geen; dat is geen fout."""
    from keycloak.exceptions import KeycloakError

    keycloak = _connector_mock()
    keycloak.set_client_authentication_flow_override.side_effect = [None, KeycloakError("Client not found")]

    await KeycloakManager(project_manager=AsyncMock())._apply_access_restriction(
        keycloak=keycloak,
        realm_name="realm-a",
        client_id="proj-app",
        restrict_access={"enabled": True, "realm_role": "allowed-user"},
    )

    keycloak.delete_authentication_flow_by_alias.assert_awaited_once()


@pytest.mark.asyncio
async def test_de_oude_flow_gaat_pas_weg_nadat_de_client_is_omgehangen() -> None:
    """Andersom staat de client even zonder poort."""
    volgorde: list[str] = []
    keycloak = _connector_mock()
    keycloak.set_client_authentication_flow_override.side_effect = lambda **kw: volgorde.append(
        f"bind:{kw['client_id']}"
    )
    keycloak.delete_authentication_flow_by_alias.side_effect = lambda **kw: volgorde.append(
        f"delete:{kw['flow_alias']}"
    )

    await KeycloakManager(project_manager=AsyncMock())._apply_access_restriction(
        keycloak=keycloak,
        realm_name="realm-a",
        client_id="proj-app",
        restrict_access={"enabled": True, "realm_role": "allowed-user"},
    )

    assert volgorde == [
        "bind:proj-app",
        "bind:proj-app-public",
        f"delete:{legacy_restricted_flow_alias('proj-app')}",
    ]


# ---------------------------------------------------------------------------
# De omleiding naar SSO Rijk hoort in de poort
# ---------------------------------------------------------------------------
#
# Deze flow hangt als browserFlowOverride op de client, en een clientoverride wint van de
# browser flow van de realm. Alles wat de blauwdruk sso-only op de REALM zet - de flow
# "External IDP Redirector" met een defaultProvider - geldt voor deze client dus niet. De
# identity-provider-redirector stond hier zonder config, en zonder defaultProvider doet die
# stap niets: de flow viel door naar het wachtwoordformulier. Een project met sso-only EN
# restrict-access kreeg daardoor het gewone inlogscherm van Keycloak (regel-k4c, 2 september
# 2026: het projectbestand zei sso-only sinds de dienst er stond, en toch verscheen er een
# inlogformulier).


@pytest.mark.asyncio
async def test_sso_only_stuurt_ook_vanuit_de_poort_door_naar_de_idp() -> None:
    connector, admin = _connector()

    await _bouw(connector, redirect_to_idp="rig-platform-oidc")

    config = _redirector_config(admin)
    assert config is not None, "de redirector in de poort heeft geen config, dus hij stuurt niemand door"
    assert config["config"] == {"defaultProvider": "rig-platform-oidc"}


@pytest.mark.asyncio
async def test_zonder_alias_blijft_het_inlogscherm_staan() -> None:
    """sso-support HOORT het scherm van Keycloak te tonen; daar mag geen omleiding op."""
    connector, admin = _connector()

    await _bouw(connector)

    assert _redirector_config(admin) is None


@pytest.mark.asyncio
async def test_de_omleiding_overleeft_opnieuw_toepassen() -> None:
    """Elke reprocess bouwt deze flow opnieuw op; de config mag niet verdubbelen of wijken."""
    connector, admin = _connector()

    await _bouw(connector, redirect_to_idp="rig-platform-oidc")
    await _bouw(connector, redirect_to_idp="rig-platform-oidc")

    configs = [c for c in admin.configs.values() if "defaultProvider" in c.get("config", {})]
    assert len(configs) == 1
    assert configs[0]["config"] == {"defaultProvider": "rig-platform-oidc"}


@pytest.mark.asyncio
async def test_de_template_bepaalt_of_de_poort_doorstuurt() -> None:
    """De manager leest de blauwdruk van het project en geeft hem door aan de poort."""
    for template, verwacht in (("sso-only", "rig-platform-oidc"), ("sso-support", None)):
        keycloak = _connector_mock()

        await KeycloakManager(project_manager=AsyncMock())._apply_access_restriction(
            keycloak=keycloak,
            realm_name="realm-a",
            client_id="proj-app",
            restrict_access={"enabled": True, "realm_role": "allowed-user"},
            template=template,
        )

        aanroep = keycloak.create_restricted_browser_flow_realm_role.await_args
        assert aanroep.kwargs["redirect_to_idp"] == verwacht, template


@pytest.mark.asyncio
async def test_omschakelen_naar_sso_support_haalt_de_omleiding_er_weer_af() -> None:
    """Van sso-only naar sso-support hoort het inlogscherm terug te komen.

    Een config die alleen bijgezet wordt en nooit weggehaald, stuurt door tot in de
    eeuwigheid: dan verandert de template in het projectbestand wel en het scherm niet.
    """
    connector, admin = _connector()

    await _bouw(connector, redirect_to_idp="rig-platform-oidc")
    await _bouw(connector)

    assert _redirector_config(admin) is None
    assert not [c for c in admin.configs.values() if "defaultProvider" in c.get("config", {})], (
        "de config hangt nergens meer aan de flow, dus hij hoort ook weg te zijn"
    )


@pytest.mark.asyncio
async def test_ook_de_clientrolpoort_stuurt_door() -> None:
    """Dezelfde poort, andere rolsoort: de omleiding hangt aan de flow en niet aan de rol."""
    connector, admin = _connector()

    await _bouw(connector, client_id="proj-app", redirect_to_idp="rig-platform-oidc")

    config = _redirector_config(admin)
    assert config is not None
    assert config["config"] == {"defaultProvider": "rig-platform-oidc"}


@pytest.mark.asyncio
async def test_een_clientrol_geeft_de_template_net_zo_goed_door() -> None:
    """De manager kent twee wegen naar de poort; ze horen allebei de blauwdruk te dragen."""
    keycloak = _connector_mock()

    await KeycloakManager(project_manager=AsyncMock())._apply_access_restriction(
        keycloak=keycloak,
        realm_name="realm-a",
        client_id="proj-app",
        restrict_access={"enabled": True, "role": "app-beheerder"},
        template="sso-only",
    )

    aanroep = keycloak.create_restricted_browser_flow.await_args
    assert aanroep.kwargs["redirect_to_idp"] == "rig-platform-oidc"
