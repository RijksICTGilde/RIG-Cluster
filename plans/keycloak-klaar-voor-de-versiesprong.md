# Keycloak klaar voor de versiesprong: Recreate, de dode opties eruit, een providertoets die iets bewijst, en een telling van de data

## Aanleiding

Gemeten op forgejo/main `3b887c9ef` (1 november 2026). Dit is plan A van drie voor kaartje #189 (Keycloak naar de laatste 26.x). Het blijft op 25.0.6 en maakt alles klaar wat vóór de sprong moet staan, zodat een storing na de sprong toe te wijzen is aan de nieuwe versie en niet aan iets dat tegelijk meeveranderde. Plan B (de sprong zelf) en plan C (inloggen via e-mail, #262) volgen later en staan hier buiten.

De eis voor de hele reeks: de sprong behoudt alle data. Realms, clients, gebruikers, wachtwoord-hashes, OTP-secrets en gekoppelde identiteiten staan alleen in de database `keycloak` op `rig-db`; realmconfiguratie kan OPI hergenereren, gebruikers en koppelingen niet.

Wat al klaar is uit #189 en dus NIET in dit plan zit: de eigen image met de providers erin (`images/keycloak-zad/Dockerfile`, `features/keycloak-eigen-image.md`), starten met `--optimized`, en `proxy-headers` op één plek (RC-212). Die staan gepind door `operations-manager/python/tests/test_keycloak_eigen_image.py`. GitHub main (de bron van productie) pint `keycloak-zad:2026.10.13.1948-72684cb4`, forgejo main `2026.10.30.1552-ce33c478`.

Wat nog open is:

1. **Geen `strategy` op de deployment.** `infrastructure/bootstrap/infrastructure/keycloak/controller/base/deployment.yaml` heeft `replicas: 1` en geen `strategy`, dus de standaard RollingUpdate. Upstream staat een rolling update bij een minor- of major-upgrade niet toe: het schema mag pas migreren als alle oude pods uit zijn. Bij RollingUpdate migreert de nieuwe pod terwijl de oude nog op het oude schema schrijft. `features/keycloak-26-upgrade.md` (Phase 3, stap 13) zegt "no rolling-upgrade concerns", en dat klopt dus niet.
2. **Vijf dode omgevingsvariabelen** in dezelfde deployment: `ORGANIZATION` (regel 108), `KEYCLOAK_ADMIN_IMPORT_ENABLED` (112), `KEYCLOAK_ADMIN_EXPORT_ENABLED` (114), `KC_HOSTNAME_PORT` (89) en `KC_PROXY_ADDRESS_FORWARDING` (91). De eerste drie zijn geen Keycloak-optie en niets in de repo leest ze (gegrept, buiten de deployment geen treffer). `KC_HOSTNAME_PORT` is een optie van hostname v1, en 25 draait standaard v2; `KC_PROXY_ADDRESS_FORWARDING` is een WildFly-restant. `KC_SPI_ADMIN_REALM_RESTAPI_EXTENSION_ENABLED` blijft: `test_keycloak_eigen_image.py:297` legt uit waarom.
3. **`/rig-metrics` faalt stil.** `keycloak-migration/custom-mapper/src/main/java/nl/minbzk/rig/keycloak/metrics/PrometheusExporter.java`, `countUsersByIdp`: breekt de JPA-query, dan telt de fallback alle gebruikers als `local` en is het antwoord 200. Een fout in de query is dan niet te onderscheiden van een realm zonder federatie. Na de sprong is precies deze query (op `keycloak-model-jpa`, een private API) het meest waarschijnlijke breekpunt.
4. **De Docker-toets van de providers bewijst niets.** `keycloak-migration/custom-mapper/test-local-docker.sh` controleert `target/keycloak-saml-nameid-mapper-1.0.0.jar` terwijl de pom op 1.1.0 staat, mount dat niet-bestaande pad op een kale `quay.io/keycloak/keycloak:26.0.0` met `start-dev`, en meldt bij een ontbrekende mapper "this is normal". Hij draait dus niet onze image, niet onze versie, en faalt nooit. Task `test-keycloak-custom-mapper-docker` (Taskfile.yaml:2090) roept hem aan.
5. **Er is geen telling om "behoud van data" mee te bewijzen.** De sprong in plan B moet aantonen dat na de migratie evenveel gebruikers, credentials per type en gekoppelde identiteiten bestaan als ervoor. Er is vandaag niets dat die aantallen geeft.

## Doel

- De deployment migreert bij een versiewissel nooit met twee pods tegelijk.
- In de deployment staat geen optie meer die niets doet.
- `/rig-metrics` laat een mislukte telling zien in plaats van hem te maskeren.
- Eén task start ONZE image zoals productie hem start en toetst per provider dat hij geregistreerd en bereikbaar is. Op 25.0.6 is dat de nulmeting; plan B draait dezelfde task op de nieuwe versie.
- Eén task geeft de aantallen uit de database `keycloak`, zonder persoonsgegevens, zodat plan B voor en na de migratie kan vergelijken.

## Plan

Code (namen, commentaar, docstrings, testnamen) in het Engels; documentatie en teksten voor de gebruiker in het Nederlands. Namen hieronder zijn voorstellen tenzij ze als bestaand worden genoemd.

### Deel 1: Recreate

1. `strategy: {type: Recreate}` in de basisdeployment, met één regel commentaar waarom (schemamigratie vraagt dat de oude pod uit is). Geen overlay zet hem terug.
   → toets: in `test_keycloak_eigen_image.py` (of een eigen testbestand ernaast als die klasse-indeling het niet draagt) een test die de basis én de drie gerenderde overlays (`odcn`, `odcn-staging`, `sandboxed-local`) leest en `Recreate` eist. Gebruik de bestaande render-hulp uit dat bestand als die er is.
2. `features/keycloak-26-upgrade.md` Phase 3 stap 13: de zin over rolling upgrades wordt vervangen door wat nu geldt (Recreate, korte onderbreking, geen twee pods op het schema).

### Deel 2: Dode opties eruit

3. De vijf variabelen uit aanleiding punt 2 gaan uit de basisdeployment. Ze staan alle vijf achter `KC_HOSTNAME` (index 4), dus de `op: test` en `replace` op `env/4` in de drie overlays blijven kloppen. Controleer dat met een render van elk van de drie overlays; de bestaande test `test_elke_omgeving_houdt_zijn_eigen_hostname` dekt dat al en moet groen blijven.
4. Meet vóór het weghalen op de sandbox wat Keycloak 25.0.6 nu over deze vijf logt bij het starten (`kubectl logs -n rig-system deploy/keycloak | grep -iE 'hostname-port|proxy-address|unknown|deprecated'`), en noteer dat in de PR. Logt hij dat `hostname-port` iets DOET, stop dan en meld het: dan is de aanname dat hij dood is verkeerd.
   → toets: een test die de env-namen van de basiscontainer leest en deze vijf weigert, met in de docstring één zin waarom elk dood is.

### Deel 3: `/rig-metrics` faalt zichtbaar

5. In `countUsersByIdp` vervalt de fallback naar `local`. Breekt de query, dan staat er voor die realm geen `rig_keycloak_users_by_idp_total` en komt er een gauge bij, voorstel `rig_keycloak_metrics_collection_error{realm="...",part="users_by_idp"} 1`, met `HELP` en `TYPE`. De buitenste catch in `exportGauges` krijgt dezelfde behandeling (`part="gauges"`) in plaats van alleen een commentaarregel. Een geslaagde telling levert voor die realm `... 0`, zodat de afwezigheid van de reeks nooit de enige aanwijzing is.
6. `PrometheusExporter` gebruikt `@SuppressWarnings` en een `Query` op de `EntityManager`; laat de query zelf ongemoeid, dit plan verandert alleen wat er bij een fout gebeurt.
   → toets: een JUnit-test (Mockito staat in de pom) waarin `createQuery` een exceptie gooit: de uitvoer bevat de foutgauge met `1` en geen `rig_keycloak_users_by_idp_total`-regel met `local` voor die realm. Een tweede test waarin de query twee rijen teruggeeft: de foutgauge staat op `0` en `local` is totaal min gefedereerd. Draai `task test-keycloak-custom-mapper`.
7. Een alert is buiten scope (zie onder); `features/prometheus-monitoring.md` krijgt wel één regel over de nieuwe gauge.

### Deel 4: Een providertoets op onze eigen image

8. `test-local-docker.sh` gaat weg. In zijn plaats een task, voorstel `test-keycloak-image`, die:
   - de image bouwt uit `images/keycloak-zad/Dockerfile` (lokaal, één platform, niet pushen);
   - hem start zoals de deployment hem start: `start --optimized` met dezelfde `--spi-...`-args als de basisdeployment, tegen een wegwerp-PostgreSQL in een eigen Docker-netwerk, met `KC_HTTP_ENABLED=true` en een hostname op localhost. Lees de args uit de deployment in plaats van ze over te schrijven, zodat deze toets niet stil afwijkt van wat productie start;
   - een pytest-module draait (voorstel `operations-manager/python/tests/integration/test_keycloak_image_providers.py`, marker `requires_infra`, overslaan zonder `KEYCLOAK_IMAGE_TEST_URL` zoals `tests/integration/test_redis_acl_echt.py` dat doet);
   - alles weer opruimt, ook als de toets faalt.
9. De pytest-module toetst per provider, via `GET /admin/serverinfo` met een admin-token uit de master-realm, dat het provider-id onder zijn SPI staat: `saml-unrestricted-xpath-idp-mapper` (identity-provider-mapper), `require-client-role-authenticator` (authenticator), `rig-metrics-listener` (eventsListener), `rig-metrics` en `always-clear-session-logout` (realm-restapi-extension), `zad-relay` (emailSender), en het thema `nl-design-system` onder de login-thema's. Lees de exacte id's uit de `META-INF/services`-bestanden en de factories, niet uit dit plan.
10. Geregistreerd is niet genoeg voor de twee endpoints, want na 26.2 kan een endpoint geladen zijn en toch 404 geven (#189). Dus ook: `GET /realms/master/rig-metrics` zonder token geeft 401 of 503 en NIET 404; met het token (zet `RIG_METRICS_TOKEN` in de container) 200 en de gauge uit deel 3 op `0`. Het logout-endpoint van `always-clear-session-logout` op zijn eigen pad geeft iets anders dan 404 (lees pad en methode uit de bron).
11. De task `test-keycloak-custom-mapper-docker` gaat weg of wordt een alias van de nieuwe; kies weghalen als niets anders hem aanroept (grep Taskfile, workflows, docs). `publish-keycloak-image` (Taskfile.yaml:2109) draait de nieuwe task vóór de push, zodat er geen image naar ghcr gaat die deze toets niet haalt.
   → toets: de task draait groen op 25.0.6 op een laptop met Docker. Tegenproef in de PR beschreven: haal één jar uit de providersmap in een tijdelijke Dockerfile-variant en laat zien dat de task rood wordt op precies die provider.
12. `features/keycloak-eigen-image.md` (sectie "Toetsen") beschrijft de task; `keycloak-migration/custom-mapper/README.md` verwijst ernaar in plaats van naar het verdwenen script.

### Deel 5: Een telling van de data

13. Een SQL-bestand, voorstel `keycloak-migration/telling.sql`, dat alleen AANTALLEN geeft, per realm: realms, clients, gebruikers (`user_entity`), gebruikers met `enabled`, credentials per `type` (password, otp, en wat er nog meer staat), `federated_identity` per `identity_provider`, identity providers, groepen, rol-toekenningen (`user_role_mapping`), required actions per actie. Geen e-mailadres, geen naam, geen id, geen hash. Gesorteerd, zodat twee uitvoeren met `diff` te vergelijken zijn.
14. Een task, voorstel `keycloak-telling`, met `CONTEXT` en `NAMESPACE` als variabelen (standaard de sandbox), die de SQL draait op de primary van `rig-db` met de EIGEN rol van Keycloak uit `keycloak-db-credentials`, nooit `postgres`. Het wachtwoord gaat via stdin, nooit als argument van `kubectl exec`, in dit patroon: `kubectl get secret ... -o jsonpath=... | base64 -d | kubectl exec -i <pod> -c postgres -- sh -c 'PGPASSWORD=$(cat) exec psql -h localhost -U <rol> -d keycloak -c "<sql>"'`. Niet `read -r PGPASSWORD`: zonder afsluitende newline stopt die keten stil. Vind de primary via het label van CNPG, niet via een vaste podnaam. De uitvoer gaat naar stdout en, met `OUT=bestand`, naar een bestand.
   → toets: op de sandbox draait de task en geeft per realm niet-nul aantallen voor gebruikers en credentials. Een pytest die het SQL-bestand leest en weigert als er een kolom in een `SELECT` staat die geen aggregaat of groeperingssleutel is (email, username, value, secret_data), zodat er nooit persoonsgegevens in de uitvoer komen.
15. `features/keycloak-26-upgrade.md` krijgt onder Phase 3 een stap vóór de dump: draai `keycloak-telling` en bewaar de uitvoer; en na de start van de nieuwe versie nog een keer, en `diff` de twee. Een verschil is een reden om terug te vallen, geen detail.

### Deel 6: Het upgradedocument bij de tijd

16. `features/keycloak-26-upgrade.md`: de status bovenaan noemt wat dit plan heeft gedaan, de tabel "Current state" klopt met de image (eigen image, `--optimized`, Recreate), en Phase 1 haalt de stappen weg die nu klaar zijn. De doelversie wordt "de laatste 26.x op het moment van plan B" in plaats van 26.7.0: die lijn krijgt geen fixes meer. Verander geen andere fases.

## Wat verandert in rechten

Niets. De telling leest met de rol die Keycloak zelf al heeft; er komt geen rol, geen grant en geen secret bij. De providertoets draait lokaal in Docker.

## Buiten scope

- De versiesprong zelf, de providers tegen 26.x bouwen, de vier metingen aan de emailSender-SPI en de generale repetitie met een productiedump: plan B.
- `KEYCLOAK_ADMIN` / `KEYCLOAK_ADMIN_PASSWORD` naar `KC_BOOTSTRAP_ADMIN_*`: die namen bestaan pas in 26; plan B.
- De providertoets in CI (`docker-images.yml`): de job bouwt multi-arch zonder `load` en draait met harden-runner op `egress-policy: block`, dus een tweede container erbij is een eigen ontwerp. Hier zit de toets in `publish-keycloak-image`, de weg waarlangs de image vandaag naar ghcr gaat.
- Een alert op `rig_keycloak_metrics_collection_error`.
- Backups van `rig-db` (#203) en het oefenen van een restore: handwerk buiten orch, wel een voorwaarde voor plan B.
- Het NLDD-thema uit #189.

## Afronding

- `cd operations-manager/python && uv run ruff check . --fix && uv run ruff format . && uv run pyright`, en de gewijzigde tests gericht draaien.
- `task test-keycloak-custom-mapper` en `task test-keycloak-image` groen; de tegenproef uit stap 11 beschreven in de PR.
- `kustomize build` van de drie Keycloak-overlays slaagt.
- Na de merge, buiten orch: `task publish-keycloak-image`, de nieuwe tag een dag op de sandbox, `task keycloak-telling` op de sandbox, dan `task pin-keycloak-image TARGET=base` en mee met de volgende release. Op productie de telling vóór en na die release draaien en vergelijken: dat is de eerste echte proef van deel 5, op een wissel zonder schemamigratie.
