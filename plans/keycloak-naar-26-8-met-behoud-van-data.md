# Keycloak naar 26.8.1 met behoud van data: de providers mee, per provider een toets, en een proefmigratie op een kopie

## Aanleiding

Gemeten op forgejo/main `33a64db5b` (1 november 2026). Dit is plan B van drie voor kaartje #189. Plan A (RC-411) is gemerged en draait op de sandbox (`keycloak-zad:2026.11.01.1851-33a64db5`, nog 25.0.6): de deployment heeft `strategy: Recreate`, `task test-keycloak-image` toetst per provider registratie en bereikbaarheid op onze eigen image, en `task keycloak-telling` geeft de aantallen uit de database `keycloak` zonder persoonsgegevens.

De eis: de sprong behoudt alle data. Realms, clients, gebruikers, wachtwoord-hashes, OTP-secrets en gekoppelde identiteiten staan alleen in de database `keycloak` op `rig-db`. De schemamigratie van Keycloak is eenrichtingsverkeer.

Doelversie: **26.8.1** (13 oktober 2026). De tag staat op quay (`quay.io/keycloak/keycloak:26.8.1`) en de artifacts op Maven Central (`org.keycloak:keycloak-services:26.8.1`). De 26.7-lijn krijgt geen fixes meer.

Wat er vandaag staat en wat de sprong raakt:

1. **Drie plekken met de versie.** `images/keycloak-zad/Dockerfile` (`FROM quay.io/keycloak/keycloak:25.0.6`), `keycloak-migration/custom-mapper/pom.xml` en `keycloak-migration/relay-email-sender/pom.xml` (`keycloak.version` 25.0.6). `test_keycloak_eigen_image.py::test_de_basisversie_is_die_waar_de_modules_tegen_bouwen` houdt ze gelijk. De relay-module draagt zijn versie ook in `RelayMailConfig.VERSION` (`RelayVersieTest`).
2. **Beide endpoints dragen `@Provider` als workaround.** `AlwaysClearSessionLogoutEndpoint.java:58` en `RigMetricsEndpoint.java:47`. Volgens #189 werkt die workaround vanaf 26.2 niet meer, met een groene build en een 404 als gevolg. `test_keycloak_image_providers.py` vangt precies dat (`test_rig_metrics_weigert_zonder_token_en_geeft_geen_404`, `test_het_logout_endpoint_is_bereikbaar`).
3. **Alle providers hangen aan interne SPI's** (`realm-restapi-extension`, `authenticator`, `identity-provider-mapper`, `eventsListener`, `emailSender`), en de metrics-query gebruikt `FederatedIdentityEntity` uit `keycloak-model-jpa`, een private API.
4. **De emailSender-SPI draagt een veiligheidseigenschap.** Zolang `zad-relay` verstuurt, leest Keycloak de `smtpServer` van geen realm, en kan een projectbeheerder de post niet omleiden (`features/keycloak-mail.md`, `docs/rc158-emailsender-spi-meting.md`). Valt de provider stil weg, dan verstuurt de standaardprovider zonder foutmelding. `features/keycloak-26-upgrade.md` noemt vier metingen die bij de sprong opnieuw moeten. 26.8.1 verandert bovendien STARTTLS op de standaardprovider: met StartTLS aan in de realmconfig weigert hij nu te versturen zonder geslaagde upgrade. OPI schrijft `starttls` mee in de realm-SMTP-config (`opi/handlers/keycloak_yaml_handler.py:89`).
5. **De opstart-admin heet in 26 anders.** De deployment zet `KEYCLOAK_ADMIN` en `KEYCLOAK_ADMIN_PASSWORD` (base `deployment.yaml:70-79`), `task test-keycloak-image` ook (Taskfile.yaml rond 2170). In 26 zijn dat `KC_BOOTSTRAP_ADMIN_USERNAME` en `KC_BOOTSTRAP_ADMIN_PASSWORD`. Ze werken alleen op een lege database, maar een nieuwe sandbox start wel leeg. De `KEYCLOAK_ADMIN_PASSWORD` van OPI zelf (`bootstrap/rig-system/.../deployment.yaml:192`) is OPI's eigen variabele en blijft.
6. **Het loginthema** is `keycloak-nl-design-system.jar` v1.4.2 van MinBZK, gebouwd voor 25. Of het op 26 rendert is niet gemeten. #189 besluit een nieuw NLDD-thema als eigen plan; dit plan maakt geen thema, maar moet wel weten of het oude nog werkt.
7. **Er is geen proef op echte data.** De telling bestaat, maar er is niets dat een kopie van de database migreert en de aantallen voor en na vergelijkt. Op de sandbox staan weinig gebruikers en geen productieachtige geschiedenis.

## Doel

- Onze image draait Keycloak 26.8.1 met alle zes providers en het thema, en `task test-keycloak-image` bewijst dat per provider, inclusief de twee endpoints en de mail-provider.
- De vier metingen aan de emailSender-SPI zitten in een herhaalbare toets, met een tegenproef.
- Eén task migreert een KOPIE van de database `keycloak` binnen het cluster naar 26.8.1 en vergelijkt de telling voor en na. Het verschil moet nul zijn. De kopie verlaat het cluster niet en de task ruimt alles op.
- `features/keycloak-26-upgrade.md` is het runbook voor het productievenster.

## Plan

Code (namen, commentaar, docstrings, testnamen) in het Engels; documentatie en teksten voor de gebruiker in het Nederlands. Namen hieronder zijn voorstellen tenzij ze als bestaand worden genoemd.

### Deel 1: De versie op drie plekken

1. `keycloak.version` naar `26.8.1` in beide poms, en `FROM quay.io/keycloak/keycloak:26.8.1` in de Dockerfile. Artifactversies omhoog: custom-mapper `1.2.0`, relay-email-sender naar de volgende minor, met `RelayMailConfig.VERSION` erbij.
2. `kc.sh build` in de Dockerfile: controleer per vlag of hij in 26.8.1 nog bestaat en een bouwoptie is (`--db`, `--health-enabled`, `--metrics-enabled`, `--features=admin-api`, `--spi-email-sender-provider`). De bouwcanarie uit RC-212 moet blijven werken: `kc.sh build` faalt op een provider-id dat niet oplost. Meet dat opnieuw op 26.8.1 met een tijdelijk fout id, en noteer de uitkomst in de PR.
   → toets: `test_keycloak_eigen_image.py` blijft groen; `task test-keycloak-custom-mapper` en `task test-keycloak-relay-email-sender` groen.

### Deel 2: De providers op 26.8.1

3. Bouw beide modules tegen 26.8.1 en repareer wat breekt. Verander niets aan het gedrag van een provider; alleen wat nodig is om hetzelfde te doen op de nieuwe API.
4. De twee endpoints: registreer ze zoals 26.8.1 het voorschrijft voor een `RealmResourceProvider` (Server Developer Guide van 26.8), en haal de `@Provider`-workaround weg als die niet meer nodig is of juist de 404 veroorzaakt. Lees dat uit de gids en de bron van 26.8.1, niet uit dit plan. Het commentaar bij beide klassen zegt daarna waarom de registratie zo staat.
5. De metrics-query (`PrometheusExporter.countUsersByIdp`, `FederatedIdentityEntity`): controleer dat entiteit en velden in 26.8.1 nog zo heten. Breekt de query, dan publiceert de foutgauge uit RC-411 een `1`.
   → toets: `test_keycloak_image_providers.py` krijgt een test die in een proefrealm een identity provider en een gebruiker met een federated-identity-koppeling aanmaakt via de admin-API, en dan op `/realms/master/rig-metrics` (met token) `rig_keycloak_users_by_idp_total{realm="<proefrealm>",idp="<alias>"} 1` en de foutgauge voor die realm op `0` ziet. Daarmee is de private-API-query op 26.8.1 gemeten, niet aangenomen.
6. De XPath-mapper en de RequireClientRoleAuthenticator: de bestaande unit tests blijven groen. Registratie dekt `test_elke_provider_is_geregistreerd`.

### Deel 3: De mail-provider, vier metingen in een toets

7. `task test-keycloak-image` start naast PostgreSQL twee SMTP-sinks in het Docker-netwerk (een image dat ontvangen berichten met headers via HTTP teruggeeft, bijvoorbeeld mailpit, gepind op een vaste versie): `relay-sink` als `ZAD_MAIL_RELAY_HOST`, en `lokaas-sink` als bestemming die een projectbeheerder zou kunnen kiezen.
8. Een nieuwe testmodule of een uitbreiding van `test_keycloak_image_providers.py` (voorstel `test_keycloak_image_mail.py`) toetst, naar `docs/rc158-emailsender-spi-meting.md`:
   - **(1) de provider wordt gebruikt:** een proefrealm met `smtpServer.host` op `lokaas-sink` en een gebruiker; een mail die Keycloak zelf verstuurt (bijvoorbeeld `PUT /admin/realms/<realm>/users/<id>/execute-actions-email`) komt aan op `relay-sink` met de header `X-ZAD-Email-Sender`;
   - **(4) de omleiding is dicht:** `lokaas-sink` heeft niets ontvangen;
   - **(2) de vlagvorm:** gedekt door `RelayEmailSenderProviderFactoryTest`, die de vorm uit `EmailSenderSpi.getName()` afleidt en dus vanzelf omvalt als die naam op 26.8.1 verandert;
   - **(3) `IdpEmailVerificationAuthenticator`:** lees in de bron van 26.8.1 of hij nog op `realm.getSmtpConfig().isEmpty()` gate, en schrijf de uitkomst in `features/keycloak-mail.md`. Verandert het, beschrijf dan wat de minimale `smtpServer` op de realms nu nog doet. Bouw daar niets voor in dit plan; meld het in de PR.
9. De STARTTLS-wijziging van 26.8.1: zet in de proefrealm `starttls: "true"` (zoals OPI hem kan schrijven) en toon dat de mail nog steeds via `relay-sink` gaat, want de relay-provider leest de realmconfig niet. Faalt dat, stop en meld het.
   → tegenproef, beschreven in de PR: dezelfde run zonder `--spi-email-sender-provider=zad-relay` bij de start laat het bericht op `lokaas-sink` landen en de test rood worden. Zonder tegenproef zegt een lege `lokaas-sink` niets.

### Deel 4: De opstart-admin

10. In de basisdeployment `KEYCLOAK_ADMIN` en `KEYCLOAK_ADMIN_PASSWORD` vervangen door `KC_BOOTSTRAP_ADMIN_USERNAME` en `KC_BOOTSTRAP_ADMIN_PASSWORD`, met dezelfde `secretKeyRef` (de sleutels in het secret blijven heten zoals ze heten, er verandert niets aan secrets). Let op: de overlays patchen `env/4` op index en de `op: test` controleert de naam `KC_HOSTNAME`; houd `KC_HOSTNAME` op index 4. `task test-keycloak-image` gebruikt dezelfde nieuwe namen.
   → toets: een test in `test_keycloak_versiesprong.py` dat de oude twee namen niet meer in de basis staan en de nieuwe wel; `test_elke_omgeving_houdt_zijn_eigen_hostname` blijft groen. `task test-keycloak-image` start op een lege database en het admin-token lukt: dat bewijst de nieuwe namen.

### Deel 5: Het thema

11. In `task test-keycloak-image`: zet het loginthema van een proefrealm op `nl-design-system` en vraag de loginpagina op (`GET /realms/<realm>/protocol/openid-connect/auth?client_id=account-console&response_type=code&redirect_uri=...`). Verwacht 200 en de loginvorm van het thema, geen foutpagina. Werkt het thema niet op 26.8.1, stop dan en meld het: dan wacht dit plan op het NLDD-thema uit #189, en dat is een besluit, geen reparatie in dit plan.

### Deel 6: Een proefmigratie op een kopie, binnen het cluster

12. Een task, voorstel `keycloak-proefmigratie`, met `CONTEXT`, `NAMESPACE` en `IMAGE` als variabelen (standaard de sandbox en de tag uit de sandbox-overlay), die in de namespace van `rig-db`:
    - een CNPG-cluster `keycloak-proef` aanmaakt (één instance, kleine opslag) met `bootstrap.initdb.import` van het type `microservice` uit `rig-db`, database `keycloak`, met de EIGEN rol van Keycloak uit `keycloak-db-credentials` als bron. CNPG 1.27 (sandbox) ondersteunt dat. De dump loopt van pod naar pod en komt niet op een laptop of in CI;
    - `keycloak-telling` draait op `keycloak-proef` VOOR de migratie (maak de cluster- en databasenaam van die task instelbaar; hij leest nu `rig-db` en `keycloak-db-credentials`);
    - een Deployment `keycloak-proef` start met de nieuwe image en exact de args van de basisdeployment, tegen `keycloak-proef`. Zonder ingress, zonder Service buiten ClusterIP, met `ZAD_MAIL_RELAY_HOST` op een niet-bestaand adres zodat er geen mail vertrekt, en zonder `RIG_METRICS_TOKEN`. Op ODCN met het rcr-pad van de image en het pull-secret, zoals de odcn-overlay van Keycloak die rendert;
    - wacht tot de pod ready is (dan is de migratie klaar) en draait `keycloak-telling` op `keycloak-proef` NA de migratie;
    - de twee tellingen met `diff` vergelijkt en faalt bij elk verschil;
    - een inlogcontrole doet op de kopie: met het bestaande admin-wachtwoord uit `keycloak-admin-credentials` een direct grant op de master-realm via een port-forward naar `keycloak-proef`. Verwacht een token, of de melding dat een tweede factor nodig is, maar NIET "invalid user credentials". Dat bewijst dat een wachtwoord-hash uit 25 op 26.8.1 nog klopt;
    - ALTIJD opruimt, ook bij een fout: Deployment, CNPG-cluster, PVC's en secrets van de proef. Het laatste wat de task doet is tonen dat er niets met `keycloak-proef` in de naam meer staat.
13. Netwerk: lees de NetworkPolicies van de namespace van `rig-db` op de sandbox en op ODCN (`infrastructure/bootstrap/infrastructure/postgresql/` en de operations-namespace-policies in `bootstrap/rig-system/`). Mag de import-pod van `keycloak-proef` niet bij `rig-db`, of de proef-Keycloak niet bij `keycloak-proef`, dan leveren de manifesten van de proef hun eigen smalle NetworkPolicy mee, op de labels van de proef, en ruimt de task die ook op. Verander geen bestaande policy. Laat de flowmatrix in `tests/netpol` groen.
14. De manifesten van de proef staan in git (voorstel `keycloak-migration/proefmigratie/`), de task past ze toe en haalt ze weg. Ze horen niet bij een ArgoCD-app, omdat ze niet mogen blijven staan.
    → toets: een pytest die de manifesten rendert en eist dat er geen Ingress of Route in staat, dat de Service ClusterIP is, dat `ZAD_MAIL_RELAY_HOST` niet naar een bestaande relay wijst en dat de args gelijk zijn aan die van de basisdeployment. De task zelf kan de worker niet draaien; dat doet een mens op de sandbox (zie Afronding).

### Deel 7: Het runbook

15. `features/keycloak-26-upgrade.md` wordt het runbook voor 26.8.1, met per stap wie het doet en wat groen is:
    - vooraf: `task test-keycloak-image` groen op de te promoveren tag; `task keycloak-proefmigratie` groen op de sandbox en op productie (zie Buiten scope over dat laatste);
    - het venster: `task keycloak-telling` op productie met `OUT=voor.txt`; een verse `pg_dump` van `keycloak` met de rol van Keycloak, die in het cluster blijft (de nachtelijke back-up van RC-278 of een handmatige run daarvan); de pin via `task pin-keycloak-image TARGET=base` en de release; wachten tot de pod ready is; `task keycloak-telling OUT=na.txt` en `diff`; een SSO Rijk-login op `rig-platform`, het logout-endpoint en `/rig-metrics` met foutgauge `0`;
    - terugvallen: wanneer (elk verschil in de telling, of een login die faalt), wie beslist, en hoe (de vorige tag terugzetten plus de dump terugzetten in een lege database `keycloak`; met Recreate draait er dan nooit een oude pod op het nieuwe schema);
    - na afloop: `adminEventsExpiration` (Phase 4 van het document) blijft een eigen stap.
16. `features/keycloak-eigen-image.md` en `features/keycloak-mail.md` bijwerken waar ze 25.0.6 of het gedrag van 25 noemen.

## Wat verandert in rechten

In git niets: er komt geen rol, grant of secret bij. De proefmigratie maakt tijdelijk een CNPG-cluster, een Deployment en eventueel een NetworkPolicy in de namespace van `rig-db`, met de rechten van wie de task draait, en ruimt ze op. De import leest met de rol die Keycloak al heeft.

## Buiten scope

- De productiewissel zelf: dat is het venster uit het runbook, met een mens erbij.
- `task keycloak-proefmigratie` op productie draaien. Die zet tijdelijk resources in de productienamespace met `kubectl apply`, en productie verandert normaal alleen via Git en ArgoCD. Dat vraagt een besluit van de eigenaar; de task maakt het mogelijk, maar beslist het niet.
- Een nieuw NLDD-thema (#189), inloggen via e-mail (#262), `adminEventsExpiration`.
- De providertoets in CI.
- Back-ups van `rig-db` in productie (#203): een voorwaarde voor het venster, geen onderdeel van dit plan.

## Afronding

- `cd operations-manager/python && uv run ruff check . --fix && uv run ruff format . && uv run pyright`, en de gewijzigde tests gericht draaien.
- `task test-keycloak-custom-mapper` en `task test-keycloak-relay-email-sender` groen.
- `kustomize build` van de drie Keycloak-overlays slaagt.
- Wat de worker niet kan draaien en een mens na de build doet, vóór de merge: `task test-keycloak-image` op 26.8.1 met de tegenproef uit stap 9, en `task keycloak-proefmigratie` op de sandbox met een `diff` van nul.
