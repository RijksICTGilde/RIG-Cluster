# De sandbox-beheerder hoeft zijn adres niet te bevestigen

## Aanleiding

Op een verse sandbox vraagt Keycloak de beheerder `admin@sandbox.rijksapp.dev` bij de eerste login om zijn e-mailadres te bevestigen. Dat adres bestaat niet als postbus. Het werkt nu alleen door de mail uit de SMTP-sink van de sandbox te vissen.

Hoe het nu gaat (gemeten op main `a6ba067fb`):

- `KeycloakSetup.setup_operations_realm` (`opi/bootstrap/keycloak_setup.py`) leest `opi/configs/projects/operations-manager-local.yaml` als `KEYCLOAK_BOOTSTRAP_CONFIG` op `local` of `sandbox` staat. De sandbox-overlay zet `KEYCLOAK_BOOTSTRAP_CONFIG=sandbox` (`bootstrap/rig-system/kustomize/operations-manager/overlays/sandboxed-local/configmap.yaml`). Productie zet hem niet, valt terug op `"default"` (`opi/core/config.py`) en leest `operations-manager.yaml`, dat geen `users` heeft.
- De gebruikers uit het lokale bestand gaan via `handler._process_users(users, variables)` naar `KeycloakYamlHandler`, die `create_user(...)` aanroept zonder `skip_email_verification`.
- `create_user` (`opi/connectors/keycloak.py`) zet dan `emailVerified: false` en `requiredActions: ["VERIFY_EMAIL"]`. Dat is bewust: sinds RC-191 bevestigt iedereen met een adres (`features/keycloak-mail.md`).
- Het commentaar in `opi/configs/keycloak/operations-manager-realm.yaml` (bij `verifyEmail`) zegt over deze beheerder letterlijk: "Die bevestigt bij aanmaak hoe dan ook (create_user)".

## De guard die hier geldt

`tests/test_keycloak_master_admins_slaan_verificatie_over.py` (RC-191) is een AST-sweep over alle `create_user`-aanroepen in `opi/`. Een aanroep die `skip_email_verification` meegeeft moet:

- een **letterlijke** `True` meegeven (`ast.Constant`),
- op een **letterlijke** realm (`realm_name` als `ast.Constant`, dus `"operations-manager"`, niet `OPERATIONS_REALM_NAME`),
- en staan in `VERWACHTE_UITZONDERINGEN`, met het juiste aantal.

Nu staan daar `("bootstrap/keycloak_setup.py", "master"): 1` en `("manager/keycloak_manager.py", "master"): 2`. Alle drie zijn master-beheerders met een adres dat niet bestaat.

**Afgewezen route:** `emailVerified` uit het YAML-gebruikersitem doorgeven in `KeycloakYamlHandler._process_users`. Die handler verwerkt de config van elke realm. Een vlag in data zou overal gelden waar een `users`-blok staat, en dat is precies wat de guard verbiedt. Een eerste versie deed dit en viel op de guard; die is teruggedraaid.

## Doel

Een verse sandbox heeft een beheerder die meteen kan inloggen, zonder bevestigingsmail. Verder verandert er aan de verificatie niets: elke andere `create_user`-aanroep bevestigt zoals nu, en de guard blijft de uitzonderingen afdwingen.

## Plan

Namen hieronder zijn voorstellen, geen bestaande code, tenzij ze als bestaand worden genoemd.

1. **Eigen aanmaak van de sandbox-gebruikers in `setup_operations_realm`.** Vervang de aanroep `handler._process_users(users, variables)` door een lus die per item `keycloak.create_user(...)` aanroept met `realm_name="operations-manager"` (letterlijke string, zie de guard) en `skip_email_verification=True`, plus een regel commentaar waarom: het adres bestaat niet als postbus, en verificatie afdwingen houdt de beheerder bij de eerste login tegen. Dezelfde formulering als bij de OTP-break-glass-admin in hetzelfde bestand.
   - Neem over wat `_process_users` met deze items doet en wat het lokale bestand gebruikt: `username`, `email`, `firstName`, `lastName`, `enabled`, het wachtwoord uit `credentials`, en een `ValueError` als het wachtwoord ontbreekt. `realmRoles`/`removeDefaultRoles` gebruikt het lokale bestand niet; controleer dat en neem ze alleen mee als ze er wel staan.
   - Gedrag bij een bestaande gebruiker blijft dat van `create_user` (409: bestaande gebruiker blijft staan, niets wordt bijgewerkt). Een sandbox die al draait verandert dus niet; het geldt vanaf de volgende verse setup.
   → toets: unit test met een `AsyncMock`-connector: `setup_operations_realm` met `KEYCLOAK_BOOTSTRAP_CONFIG=sandbox` roept `create_user` aan met `realm_name="operations-manager"` en `skip_email_verification=True`; met `"default"` wordt er geen gebruiker aangemaakt.
2. **De guard bijwerken.** Voeg `("bootstrap/keycloak_setup.py", "operations-manager"): 1` toe aan `VERWACHTE_UITZONDERINGEN` en leg in de docstring uit waarom deze uitzondering bestaat: alleen bereikbaar via de lokale/sandbox-bootstrap, een bestand van het platform zelf, een adres dat niet bestaat.
   → toets: `tests/test_keycloak_master_admins_slaan_verificatie_over.py` is groen, en faalt als je de nieuwe aanroep `skip_email_verification` afneemt of hem op `OPERATIONS_REALM_NAME` zet.
3. **Commentaar rechtzetten** in `opi/configs/keycloak/operations-manager-realm.yaml`: de zin dat deze beheerder bij aanmaak bevestigt klopt niet meer.
4. **In de sandbox meten.** Op een verse sandbox (`orch sandbox claim`): na de setup heeft de gebruiker `admin` in realm `operations-manager` `emailVerified: true` en geen `VERIFY_EMAIL` in `requiredActions`. Opvragen kan met `kcadm.sh get users -r operations-manager --fields username,email,emailVerified,requiredActions` in de Keycloak-pod.

## Wat hierdoor schrijfbaar wordt

Niets dat een tenant kan raken. De uitzondering zit in één codepad (`setup_operations_realm`), leest alleen het gebundelde `operations-manager-local.yaml` uit `opi/configs/projects/` en draait alleen als `KEYCLOAK_BOOTSTRAP_CONFIG` op `local` of `sandbox` staat. Productie zet die variabele niet. De guard is de test uit stap 2: een tweede aanroep met de vlag, of deze aanroep op een andere realm, valt daarop.

## Buiten scope

- Andere realms of gebruikers. Alleen de sandbox-beheerder.
- Een sandbox die al draait bijwerken. `create_user` laat een bestaande gebruiker staan.
- De Taskfile-variant (na de setup met `kcadm.sh update` op verified zetten). Afgewezen omdat de kennis over deze gebruiker dan op een tweede plek staat.

## Afronding

`uv run ruff check . --fix`, `uv run ruff format .`, `uv run pyright` vanuit `operations-manager/python`. Geen em dashes. Nederlandse prose met de Engelse vaktermen uit de woordenlijst in `CLAUDE.md`.
