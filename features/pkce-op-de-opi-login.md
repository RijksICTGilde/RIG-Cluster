# PKCE op de OPI-login

## Wat het is

De login van de Operations Manager zelf (`/auth/login`) wisselt een authorization code om
bij Keycloak. Die wissel gebruikt sinds RC-324 PKCE met S256, en de OPI-client in Keycloak
dwingt het af: een code zonder `code_verifier` wordt geweigerd. Daarmee is een onderschepte
code op zichzelf niet meer genoeg om een token te krijgen, ook niet als het clientsecret
elders uitlekt. Het NL GOV OIDC-profiel vraagt PKCE ook voor een confidential client.

## Hoe het werkt

Twee kanten, die beide mee moeten:

1. **De kant van OPI.** `register_oauth_client_after_keycloak_setup`
   (`opi/core/startup.py`) geeft authlib `client_kwargs={"code_challenge_method": "S256"}`
   mee. Authlib bewaart dan de verifier in de sessie en zet `code_challenge` en
   `code_challenge_method` in de authorize-redirect.
2. **De kant van Keycloak.** `KeycloakSetup.setup_operations_client`
   (`opi/bootstrap/keycloak_setup.py`) maakt de client `rig-platform-operations-manager`
   met `require_pkce=True`. `create_deployment_client` zet dan
   `pkce.code.challenge.method: S256` in `expected_attributes`, en omdat die verzameling ook
   in de 409-tak wordt vergeleken en bijgewerkt, krijgt een client die er al was het
   attribuut bij de volgende start van OPI.

De clients van projectdeployments veranderen niet: `require_pkce` staat daar op `False`,
want of de applicatie PKCE kan sturen beslist de applicatie. De `-public`-zus van zo'n
client had S256 al.

## Afhankelijkheden

- authlib (via `opi.core.startup`), Keycloak-attribuut `pkce.code.challenge.method`.
- Toetsen: `tests/test_keycloak_platform_zonder_ontwikkelresten.py`.
