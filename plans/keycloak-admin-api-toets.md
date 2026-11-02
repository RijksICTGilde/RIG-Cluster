# Een toets voor de Keycloak-admin-API zoals OPI hem gebruikt, tegen een echte Keycloak

## Aanleiding

Gemeten op forgejo/main `4c759a27d` (2 november 2026), direct na de merge van RC-412. De sprong naar Keycloak 26.8.1 is gemeten op de providers, de mail, het thema, de uitlogketen en een proefmigratie op een kopie. Eén ding is niet gemeten: de admin-API die OPI zelf gebruikt. OPI richt elk project-realm in via die API (realms, clients, identity providers, authenticatieflows, rollen, gebruikers, OTP). Breekt daar iets, dan merk je dat pas als een projectbeheerder een deployment aanmaakt.

Wat er vandaag staat:

1. **De connector.** `opi/connectors/keycloak.py`, klasse `KeycloakConnector`, op `python-keycloak` 7.1.1 (`uv.lock`). Er zijn 64 publieke methodes. 55 daarvan worden buiten de connector aangeroepen, 4 alleen binnen de connector (`get_discovery_url`, `get_client_scopes`, `assign_client_scope_to_client`, `assign_client_scope_as_realm_default`). Vier hebben geen enkele aanroeper in `opi/`: `update_deployment_client_hosts`, `ensure_standard_oidc_mappers`, `assign_realm_management_role`, `assign_realm_role_to_user`.
2. **Twee inlogvormen.** `create_keycloak_connector` kiest client-credentials met de service account `opi-admin-service` als `KEYCLOAK_ADMIN_CLIENT_SECRET` gezet is, anders het adminwachtwoord. Productie draait op client-credentials. `KeycloakSetup.ensure_master_admin_service_account` (`opi/bootstrap/keycloak_setup.py:217`) maakt die client bij de eerste start met het adminwachtwoord aan. De rechten van die service account zijn dus wat productie gebruikt, niet die van `admin`.
3. **De aanroepers.** `opi/bootstrap/keycloak_setup.py` (`KeycloakSetup.setup_all` en de `ensure_*`-stappen), `opi/manager/keycloak_manager.py` (`create_resources_for_deployment`, `delete_resources_for_deployment`, `handle_service_removal`), `delete_project_manager.py`, `invite_manager.py`, `db_console_manager.py`, `jobs/reconciliation.py`, `jobs/service_orphan_sweep.py`, `core/db_console_reaper.py`.
4. **De bestaande toetsen mocken Keycloak.** De ruim dertig `tests/test_keycloak_*.py` toetsen de payload die OPI bouwt tegen een nagebootste `KeycloakAdmin`. Of Keycloak die payload accepteert en er ook naar handelt, toetst geen enkele.
5. **Er staat al een echte Keycloak klaar.** `task test-keycloak-image` start onze eigen image met de args uit de basisdeployment, tegen PostgreSQL, en draait `tests/integration/test_keycloak_image_providers.py` en `test_keycloak_image_mail.py` met `-m requires_infra`. Hulpen staan in `tests/integration/keycloak_image.py` (`omgeving`, `basis_url`, `admin_token`, `tijdelijke_realm`). `publish-keycloak-image` draait die task vóór de push.

## Doel

- Elke connectormethode die OPI aanroept, draait minstens één keer tegen een echte Keycloak. Na elke schrijvende aanroep leest de toets terug wat Keycloak opsloeg en vergelijkt hij dat met wat OPI bedoelde. "Geen exceptie" is geen bewijs: Keycloak negeert onbekende velden stil.
- De toetsen draaien met dezelfde inlogvorm als productie: de service account `opi-admin-service`, aangemaakt door OPI's eigen bootstrap.
- De toets hangt in `task test-keycloak-image`, dus geen image komt meer op ghcr zonder dat OPI's gebruik van de admin-API ertegen gemeten is.

## Plan

Code (namen, commentaar, docstrings, testnamen) in het Engels; documentatie in het Nederlands. Namen hieronder zijn voorstellen, tenzij ze als bestaand genoemd worden.

### Deel 1: Inventaris als toets

1. Een statische toets (voorstel `tests/test_keycloak_admin_api_coverage.py`, gewone run, geen infra) die met `ast` de publieke methodes van `KeycloakConnector` opsomt en de methodes die buiten de connector in `opi/` worden aangeroepen. Elke aangeroepen methode moet in de integratietoets van deel 3 voorkomen, of in een expliciete lijst met uitzonderingen en per uitzondering een reden. Komt er een nieuwe aanroep bij zonder integratietoets, dan wordt deze toets rood.
   → toets: rood maken door een methode uit de integratietoets te halen, en terugzetten.
2. De vier methodes zonder aanroeper vallen buiten de dekking. De toets noemt ze in een lijst, met als reden "geen aanroeper in opi/". Weghalen hoort niet bij dit plan; noem ze in de PR.

### Deel 2: De opstelling

3. Een fixture (in `tests/integration/keycloak_image.py` of een conftest ernaast) die tegen de Keycloak van `task test-keycloak-image` eerst `KeycloakSetup.ensure_master_admin_service_account` draait, met `settings` gepatcht op die Keycloak en een wegwerp-clientsecret. Daarna levert hij een `KeycloakConnector` in client-credentials-modus via `create_keycloak_connector()`. Zo meet de toets meteen de bootstrap van productie, en draait alles daarna met de rechten van de service account.
4. Elke toets werkt in een eigen wegwerprealm met een vaste prefix (voorstel `adminapi-`), en een fixture ruimt die altijd op, ook bij een fout.
5. Guard: de module weigert te draaien als `KEYCLOAK_IMAGE_TEST_URL` niet naar `localhost` of `127.0.0.1` wijst. Deze toets maakt realms aan en hoort nooit tegen een gedeelde of productie-Keycloak te lopen.

### Deel 3: Per methode, met terugleesbewijs

6. Een integratiemodule (voorstel `tests/integration/test_keycloak_admin_api.py`, marker `requires_infra`), gegroepeerd per domein. Per methode: aanroepen zoals de echte aanroeper het doet (lees de aanroep in `opi/` en gebruik dezelfde argumenten), dan via de admin-API teruglezen. Wat er minstens teruggelezen wordt:
   - **realm:** `create_realm`, `update_realm_settings`, `remove_default_role`, `set_required_action_enabled`/`_default`: de realmrepresentatie en de required actions zoals OPI ze zette.
   - **clients:** `create_oidc_client`, `create_deployment_client`, `delete_deployment_client`, `regenerate_client_secret`, `get_client_secret`, `create_custom_client_scope`, `list_client_ids_by_prefix`, `delete_client_by_client_id`: redirect-URI's, web origins, client-scopes, protocolmappers, en dat het nieuwe secret anders is dan het oude.
   - **identity providers:** `add_identity_provider`, `add_saml_identity_provider`, `update_identity_provider`, de mapper-methodes: config, `syncMode`, en de mappers met hun type (inclusief `saml-unrestricted-xpath-idp-mapper` uit onze eigen image).
   - **flows:** `ensure_browser_flow`, `create_restricted_browser_flow`, `create_restricted_browser_flow_realm_role`, `set_client_authentication_flow_override`, `configure_sso_redirect_flow`, `ensure_auto_link_first_broker_login_flow`, `ensure_required_otp_browser_flow`, `ensure_configure_totp_required`, `create_post_broker_login_flow` en `_realm_role`, `set_identity_provider_post_broker_login_flow`, `delete_authentication_flow_by_alias`: de executions van de flow in volgorde, met requirement en authenticator (inclusief `require-client-role-authenticator` uit onze image). De flowstructuur is het stuk dat 26 het vaakst verbouwt.
   - **rollen, groepen, gebruikers:** `create_realm_role`, `create_client_role`, `assign_*`, `create_group`, `join_user_to_group`, `remove_all_realm_roles`, `create_user` (met en zonder `totp_secret`), `get_user_by_username`/`_email`, `delete_user_by_username`, `send_verify_email`, `assign_realm_admin_from_master`: rolmappings, groepslidmaatschap, credentials per type, required actions op de gebruiker.
   - **lezen en verwijderen:** `realm_exists`, `get_realm`, `list_realms`, `list_clients`, `get_identity_providers`, `find_client_by_client_id`, `realm_discovery_available`, `connection_works`, `delete_realm`.
7. **Idempotentie.** Elke `ensure_*`-methode en elke methode die een aanroeper bij een tweede run opnieuw aanroept, draait twee keer. Na de tweede run is de teruggelezen toestand gelijk aan die na de eerste: geen dubbele executions, mappers of rollen.
8. `send_verify_email` verstuurt via de relay-sink van `task test-keycloak-image`. Toets dat het bericht daar aankomt; dat sluit aan op `test_keycloak_image_mail.py`.

### Deel 4: Twee echte stromen

9. `KeycloakSetup.setup_all` tegen de lege Keycloak, twee keer achter elkaar, met `settings` gepatcht. Na de eerste run staan het operations-realm, de OPI-client, de OTP-beheerder (als de seed gezet is) en de verharding van master zoals de bootstrap ze bedoelt. De tweede run verandert niets.
10. `KeycloakManager.create_resources_for_deployment` en daarna `delete_resources_for_deployment` voor één deployment uit een voorbeeldproject. Alles wat geen Keycloak is (git, secrets, kubernetes via `ProjectManager`) mag een fake zijn; Keycloak zelf nooit. Kost dat meer dan fakes voor de schrijfmethodes van `ProjectManager`, dan dekt deel 3 dezelfde aanroepen en zeg je in de PR waarom de stroom er niet in zit.

### Deel 5: In de task en in de documentatie

11. Voeg de nieuwe module toe aan de pytest-aanroep in `task test-keycloak-image` (Taskfile, rond regel 2232). De task blijft één commando.
12. Documenteer in `features/keycloak-eigen-image.md` wat deze toets dekt en wat niet, en in `features/keycloak-26-upgrade.md` dat deze toets bij elke versiesprong groen moet zijn vóór de promotie.

## Gates en klaar

- `ruff`, `ruff format`, `pyright` schoon; de gewone pytest-run groen (de dekkingstoets uit deel 1 draait daarin).
- De integratiemodule moet aantoonbaar rood kunnen worden. Laat in de PR één voorbeeld zien: een veld in een payload tijdelijk verkeerd gespeld, waarop de terugleestoets faalt.
- Kan de worker `task test-keycloak-image` niet draaien (bij RC-412 kon een sessie de image niet bouwen), schrijf dat dan in de PR. Draai de module dan wel tegen een kale `quay.io/keycloak/keycloak:26.8.1` in Docker als dat lukt, en zeg welke toetsen daar overgeslagen zijn omdat ze een provider uit onze image nodig hebben.

## Buiten scope

- Een toets tegen de sandbox- of productie-Keycloak. De guard uit stap 5 verbiedt dat.
- De vier methodes zonder aanroeper weghalen.
- De OIDC-tokenaanroepen buiten de connector (`opi/api/invite_routes.py`, `opi/core/keycloak_client_startup.py`). Dat is standaard-OIDC en geen admin-API.
- Iets veranderen aan het gedrag van de connector. Breekt een methode op 26.8.1, dan is de reparatie in dit plan toegestaan als ze klein is en de toets haar bewijst. Is ze groter, stop dan en meld het: dan wacht de productie-uitrol van 26.8.1 erop.

## Na de build, door een mens

- `task test-keycloak-image` op de gemergede main (26.8.1) groen, inclusief de nieuwe module.
