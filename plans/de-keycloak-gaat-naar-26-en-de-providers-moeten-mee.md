# De Keycloak gaat naar 26.7.4, en de providers moeten mee

**Status**: plan, nog niets gebouwd.
**Datum**: 2026-09-26
**Aanleiding**: het platform draait `quay.io/keycloak/keycloak:25.0.6` uit september 2024. Keycloak onderhoudt alleen de nieuwste minor, dus we staan twee jaar buiten de beveiligingsstroom: 26.7.4 alleen al (uitgebracht 16 september 2026) repareert zes CVE's. RC-212 heeft de weg vrijgemaakt door de basisversie naar één plek te halen (`images/keycloak-zad/Dockerfile:25`) en de tag per omgeving te pinnen, waardoor een upgrade nu een imagebouw met een proef ervoor is in plaats van een regel in een manifest.
**Context**: `features/keycloak-26-upgrade.md` (het bestaande migratiedocument), `features/futures/keycloak-26-upgrade.md` (de eerdere fasering en de risicolijst), `features/keycloak-eigen-image.md` (bouwen, pinnen, promoveren), `features/keycloak-mail.md` en `docs/rc158-emailsender-spi-meting.md` (de emailSender-SPI en de meetopstelling ervan), `features/keycloak-always-clear-session-logout.md` en `features/keycloak-rig-metrics.md` (de verificatierecepten van die twee endpoints). Dit plan bouwt op RC-212, de eigen Keycloak-image.
**Overgezet op 2026-10-12 (RC-260).** De paragraaf "Wat er vandaag staat, gemeten" en de
regelnummers in `base/deployment.yaml` die daarop volgen beschrijven de staat van voor die
merge: de laatste rondes van RC-212 hebben de vlagverdeling, `--proxy-headers` en de
env-indexen al verzet. Meet die staat opnieuw voor je er een beslissing op bouwt; de analyse
per provider en per optie staat daar los van.

## Wat hier niet opnieuw staat

| Staat al ergens | Wat dit plan ermee doet |
|---|---|
| Waarom we upgraden, de huidige-toestandstabel, de OPI-afhankelijkheden | `features/keycloak-26-upgrade.md:7-37` en `:164-171`. Niet herhaald. |
| De vier metingen van de emailSender-SPI, met de aanvalsproef en de tegenproef | `features/keycloak-26-upgrade.md:39-67`, meetopstelling in `docs/rc158-emailsender-spi-meting.md`. Dit plan neemt ze over als één regel in "Klaar als" en schrijft ze niet uit. |
| `adminEventsExpiration` na de upgrade | `features/keycloak-26-upgrade.md:153-162`. Blijft daar, valt buiten deze migratie. Eén nuance erbij, zie "Valkuilen". |
| Het verificatierecept van het logout-endpoint en van `/rig-metrics` | de paragraaf `## Verification` in `features/keycloak-always-clear-session-logout.md`, `features/keycloak-rig-metrics.md:17-20`. Hier alleen naar verwezen. |
| Scope, out-of-scope en de risicolijst op hoofdlijnen | `features/futures/keycloak-26-upgrade.md`. Dit plan is de uitvoerbare versie ervan, niet een tweede lijst. |

## Wat dit plan toevoegt

1. **Doelversie 26.7.4**, niet 26.7.0 zoals `features/keycloak-26-upgrade.md:1` nog noemt. 26.7 is vandaag de enige lijn met beveiligingsonderhoud; er is geen 27.x, de volgende release in de pijplijn is 26.8.
2. **Zes providers, niet vier.** Beide bestaande documenten tellen "vier SPI's in de custom-jar" plus de emailSender. De pod laadt er vandaag zes, over vijf SPI's, en twee ervan zitten op dezelfde SPI (`realm-restapi-extension`). De toets hoort per provider te gaan, niet per bullet.
3. **Een compilatie bewijst hier bijna niets.** De twee API's waarvan je zou verwachten dat ze omvallen, zijn in 26 gedeprecieerd en niet verwijderd. Ze blijven dus compileren en kunnen alsnog van betekenis veranderen. Zie de tabel hieronder.
4. **Waar de waarheid van de opties hoort te liggen**, met een advies en met de gevolgen voor de overlays.
5. **De pod moet uit tijdens de migratie**, en dat is vandaag niet geregeld. Upstream: rolling updates zijn niet mogelijk bij een minor- of major-upgrade, en de database mag pas migreren als alle oude nodes uit zijn. De deployment heeft geen `strategy`, dus de standaard RollingUpdate laat de 26-pod het schema migreren terwijl de 25-pod nog loopt.
6. **Een terugvalweg die bestaat**, in plaats van een verwijzing naar een backup die er niet is.
7. **Twee toetsen die vandaag niets bewijzen** en die dus geen bewijs mogen worden: `keycloak-migration/custom-mapper/test-local-docker.sh` en het fail-soft gedrag van de metriekenexport.

En wat het tegenspreekt:

- `features/keycloak-26-upgrade.md:116-118` zegt dat `--spi-theme-welcome-theme` in 26 `--spi-theme--welcome-theme` wordt omdat de enkelstreepvorm verouderd is. Dat is **niet vastgesteld**: de dubbelstreepvorm `--spi-<spi>--<provider>--<eigenschap>` bestaat al vóór 26 en er is in de 26-changelogs geen wijziging van de optievorm gevonden. RC-158 heeft de vorm `--spi-email-sender-provider=` op 25.0.6 gemeten. Behandel de vorm dus als **te meten op 26.7.4**, niet als een verplichte herschrijving.
- `features/keycloak-26-upgrade.md:106-108` zegt dat `KEYCLOAK_ADMIN` en `KEYCLOAK_ADMIN_PASSWORD` omgezet moeten worden. Ze zijn in 26 gedeprecieerd ten gunste van `KC_BOOTSTRAP_ADMIN_USERNAME` en `KC_BOOTSTRAP_ADMIN_PASSWORD`, maar niet verwijderd. Doe het wel, en weet dat het geen blokkade is: een mislukte upgrade zal hier niet aan liggen.
- `features/keycloak-26-upgrade.md:104` zegt "base image in de Dockerfile, dan bouwen en pinnen". Dat is inmiddels de enige weg en geen stap in fase 1; hij hoort in fase 0 bij de poms, want een toets pint die drie waarden aan elkaar (de toets `test_de_basisversie_is_die_waar_de_modules_tegen_bouwen` in `operations-manager/python/tests/test_keycloak_eigen_image.py`).

## Wat er vandaag staat, gemeten

Vandaag op de sandbox, eigen image `sha-8ecff078a`, Keycloak 25.0.6:

- **Alle zes providers laden, en bij elk van hen waarschuwt Keycloak dat hij een INTERNE SPI implementeert die zonder aankondiging kan wijzigen**: `always-clear-session-logout` en `rig-metrics` op `realm-restapi-extension`, `require-client-role-authenticator` op `authenticator`, `saml-unrestricted-xpath-idp-mapper` op `identity-provider-mapper`, `rig-metrics-listener` op `eventsListener`, `zad-relay` op `emailSender`.
- **De pod augmenteert bij het starten alsnog** ("Changes detected in configuration", 5906 ms), omdat de runtime andere opties vraagt dan de `kc.sh build`-regel in `images/keycloak-zad/Dockerfile` heeft vastgelegd. Dat wordt in een aparte reworkronde op RC-212 opgelost en is geen onderdeel van deze migratie, maar het is wel een voorwaarde eraan: zie "Volgorde".
- **De proxy-instelling spreekt zichzelf tegen.** De args zetten `--proxy-headers=forwarded` (`base/deployment.yaml:53-54`), de env zet `KC_PROXY_HEADERS=xforwarded` (`:66-67`). Keycloak geeft de commandoregel voorrang boven de omgeving, dus wat er vandaag geldt is `forwarded`, terwijl nginx en HAProxy `X-Forwarded-*` sturen. Gevolg: Keycloak ziet niet het IP van de bezoeker maar dat van de proxy, en dat zit in de events en in de brute-force-detectie.
- **Productie heeft de eigen image nog nooit gedraaid.** Op main draagt `infrastructure/bootstrap/infrastructure/keycloak/controller/base/deployment.yaml:67` nog `quay.io/keycloak/keycloak:25.0.6` met initContainers; op deze tak staat op `:39` de eigen image, met de tag die met de merge naar productie gaat.

## De providers, per stuk

Per provider: wat hij van binnenuit Keycloak gebruikt, wat daar in 26 mee gebeurd is, en de toets die hem op 26 bewijst. "Compileert" betekent hier alleen: `mvn -B package` met `keycloak.version` op 26.7.4 loopt door. Dat is bij drie van de zes geen bewijs.

| Provider (SPI) | Wat hij aanroept | Toestand in 26 | Toets die het bewijst |
|---|---|---|---|
| `saml-unrestricted-xpath-idp-mapper` (`identity-provider-mapper`) | `AbstractIdentityProviderMapper`, de contextsleutel `SAMLEndpoint.SAML_ASSERTION` (`UnrestrictedXPathAttributeMapper.java:214`), `AssertionUtil.asDocument` (`:221`), `SAMLIdentityProviderFactory.PROVIDER_ID` (`:104`) | `IdentityProviderMapper` en `AbstractIdentityProviderMapper` zijn in 26.0 tot 26.7 niet gewijzigd. De refactor van 26.5/26.6 raakt de `IdentityProvider`-SPI, niet de mapper | Een echte SSO-Rijk-login op de sandbox, en daarna het gebruikersattribuut bekijken. Zonder deze mapper is er geen NameID en breekt het inloggen, dus dit is de eerste toets en niet de laatste |
| `always-clear-session-logout` (`realm-restapi-extension`) | `realm.getIdentityProviderByAlias` (`AlwaysClearSessionLogoutEndpoint.java:72`), `authenticateIdentityCookie` (`:87`), de zevenargumentige `backchannelLogout` (`:97`), `CookieProvider` en `CookieType` (`:110-113`) | `backchannelLogout` en `authenticateIdentityCookie` zijn ongewijzigd. `getIdentityProviderByAlias` is GEDEPRECIEERD ten gunste van `IdentityProviderStorageProvider.getByAlias`, dus het compileert en het is wel het stuk dat 26.0 heeft verbouwd (identity providers zijn uit de realmrepresentatie gehaald). `AuthResult` is sinds 26.5 een record en `getSession()` is gedeprecieerd, ook compilerend | Het recept uit de paragraaf `## Verification` van `features/keycloak-always-clear-session-logout.md`: eerst de 404 met platte tekst op een niet-bestaande alias, dan een echte logout waarna de sessie uit `GET /admin/realms/<realm>/users/{id}/sessions` verdwenen is. Alleen de 404 is niet genoeg: die raakt de gedeprecieerde lookup wel, maar niet de logoutweg |
| `rig-metrics` (`realm-restapi-extension`) | JPQL tegen `FederatedIdentityEntity` via `JpaConnectionProvider` (`PrometheusExporter.java:241-247`), `session.users().getUsersCount` (`:208`, `:267`) | `FederatedIdentityEntity` heeft in 26.7 nog dezelfde velden, `getUsersCount` bestaat nog, en `keycloak-model-jpa` wordt voor 26.7.4 nog gepubliceerd | **Kijk naar de inhoud, niet naar de statuscode.** Deze export faalt SOFT: de catch op `PrometheusExporter.java:273-282` telt bij een kapotte query iedereen als `idp="local"` en de endpoint geeft alsnog 200. De toets is een realm met een gefedereerde gebruiker die een regel `rig_keycloak_users_by_idp_total{...,idp="sso-rijk"}` oplevert, plus een log zonder "Error counting users by IDP" |
| `rig-metrics-listener` (`eventsListener`) | `PostMigrationEvent` (`RigMetricsEventListenerFactory.java:39`), `RealmModel.RealmCreationEvent` (`:50`), en het SCHRIJFT bij het starten op elke realm (`setEventsListeners`, `:67`) | Geen van de 26-changelogs noemt deze SPI of deze twee provider-events. Wel relevant: dit is de enige provider die tijdens de startup transacties doet, en in dezelfde startup draait bij deze upgrade de schemamigratie | Na een verse start staat de listener op een nieuwe realm, en een login verhoogt `rig_keycloak_logins_total`. Valt de auto-registratie stil, dan is er geen fout: de tellers blijven alleen op nul |
| `require-client-role-authenticator` (`authenticator`) | `Authenticator` en `AuthenticatorFactory`, en de faalweg rendert via het thema (`RequireClientRoleAuthenticator.java:88-91`, `context.form().setError(...).createErrorPage(...)`) | De `authenticator`-SPI is niet gewijzigd. De faalweg hangt aan het thema, en daar zit in 26 juist wel iets, zie de themaparagraaf | Een gebruiker zonder de rol krijgt de gestileerde 403 met de vertaalde melding, geen ruwe sleutel en geen stacktrace. Een gebruiker mét de rol komt binnen, zodat de tegenproef er ook is |
| `zad-relay` (`emailSender`) | `EmailSenderProvider.send(Map, String, ...)`, plus het merkteken `X-ZAD-Email-Sender` (`RelayEmailSenderProvider.java:67`) | `EmailSenderProvider` en `EmailSenderProviderFactory` zijn tussen 25.0.6 en 26.7.x identiek. Het risico is dus niet de vorm van de interface maar het gedrag: wordt hij in productiemodus echt gebruikt, en wijst de vlag hem nog aan | De vier metingen uit `features/keycloak-26-upgrade.md:51-63`, ongewijzigd, inclusief de aanvalsproef EN de tegenproef. Bewijs is het merkteken op het bericht in de sink, niet een bericht dat aankomt |

De Java-verwijzingen hierboven zijn relatief aan `keycloak-migration/custom-mapper/src/main/java/nl/minbzk/rig/keycloak/` en, voor de relay, aan `keycloak-migration/relay-email-sender/src/main/java/nl/minbzk/rig/keycloak/email/`.

Twee dingen die voor alle zes gelden en die per provider geen aparte regel hoeven:

**Het gat tussen "compileert" en "werkt" zit bij deze zes vooral in de JAX-RS-registratie.** Beide `realm-restapi-extension`-providers dragen `@Provider` op hun binnenste resourceklasse omdat RESTEasy in de Quarkus-distributie de JAX-RS-annotaties zonder hem stil overslaat, met een 404 zonder diagnose; die reden staat in `AlwaysClearSessionLogoutEndpoint.java:52-59` met een verwijzing naar keycloak#25882, dat het als 405 rapporteerde. In een discussie bij dat issue staat dat die workaround vanaf 26.2.0 niet meer werkt. Dat is één losse melding en geen changelogregel, dus behandel het als het eerste dat je nagaat bij een 404 of 405 op een endpoint dat volgens de log wel geladen is.

**Java 17 blijft genoeg.** Keycloak 26.7.4 compileert zijn eigen bronnen nog met `maven.compiler.release 17`, dus `maven.compiler.source/target 17` in beide poms (`custom-mapper/pom.xml:16-17`) kan blijven staan. De bouwfase gebruikt al `maven:3.9-eclipse-temurin-21` (`images/keycloak-zad/Dockerfile:8`) en de officiële container draait op JDK 21.

## Het thema is een gate, geen detail

`images/keycloak-zad/Dockerfile:20-23` haalt `keycloak-nl-design-system.jar` v1.4.2 van MinBZK op, met een sha256 ernaast. Keycloak 26.0 heeft het Login V2-thema geïntroduceerd (`keycloak.v2`) en het oude `keycloak`-thema gedeprecieerd, met een herziene HTML- en CSS-structuur; themes die op de oude structuur leunen zijn daar niet mee compatibel, en 26.7 heeft daarbovenop nog template-wijzigingen in de loginpagina's. Aan dat thema hangen drie dingen die we zelf gebruiken: het inlogscherm, het welkomstthema (`--spi-theme-welcome-theme=nl-design-system`) en de foutpagina van de rolpoort hierboven.

Ga dus vóór de imagebouw na of MinBZK/keycloak-theme een release heeft die 26 noemt. Is die er niet, dan is dit een keuze die de opdrachtgever maakt en niet de bouwer: wachten op een release, zelf een release met `parent=base` voorstellen bij MinBZK, of de upgrade doorzetten met een inlogscherm dat er anders uitziet. Doorzetten zonder die keuze is het gat waarlangs het eerste dat een gebruiker van dit platform ziet, stukgaat.

## De opties: waar de waarheid hoort te liggen

De opties staan vandaag op vier plekken: ingebakken in `kc.sh build` in de Dockerfile, als `args` in de deployment (`base/deployment.yaml:53-54`), als `env` in diezelfde deployment (`:56-146`), en als JSON-patch in elke overlay. Twee ervan spreken elkaar al tegen, en de augmentatie bij elke podstart is het zichtbare gevolg van dezelfde verspreiding.

Alle manifestverwijzingen in deze paragraaf zijn relatief aan `infrastructure/bootstrap/infrastructure/keycloak/controller/`.

**Voorstel, drie regels:**

1. **Een build-time-optie staat alleen in de Dockerfile.** Dat zijn `db`, `health-enabled`, `metrics-enabled`, `features` en de providerkeuzes. Staat dezelfde waarde ook in de runtime-omgeving, dan augmenteert de pod alsnog; dat is precies wat er vandaag gebeurt. De RC-212-reworkronde beslist hoe, deze migratie erft die beslissing.
2. **Een runtime-optie die op elk cluster hetzelfde is, staat als `KC_`-variabele in de basis, en niet in `args`.** `args` is één lijst die een overlay alleen in zijn geheel kan vervangen; een env-lijst wordt op naam samengevoegd, en `overlays/odcn/patch-mail-relay-host.yaml:16-18` leunt daar al op. Wat er in `args` overblijft is `["start"]`.
3. **Een verschil per cluster staat in de overlay, als merge-patch op de NAAM van de variabele.** Vandaag doet elke overlay dat met een JSON-patch op index 4, met een `op: test` op de naam van die index ervoor. Die guard doet zijn werk, maar hij pint de ORDE van de eerste vijf variabelen vast: haal je `KC_PROXY_HEADERS` weg, dan schuift `KC_HOSTNAME` naar index 3 en faalt elke overlay bij `kustomize build`. Luid falen is goed, hem per overlay moeten meeverhuizen is onnodig. De mail-relay-patch laat zien hoe het zonder index kan.

Per variabele uit de gemeten podomgeving:

| Variabele | Wat ermee moet | Waarom |
|---|---|---|
| `KC_HOSTNAME` | Blijft, als volledige URL. De basis draagt op `:74-75` nog `keycloak.kind`, en dat is de laatste v1-vorm in de repo; de overlays zetten er al een `https://`-URL over. Zet de basis gelijk aan de overlays of laat de basis geen waarde dragen | Hostname v2 accepteert een volledige URL en dat is de enige vorm waarin pad en poort nog uit te drukken zijn |
| `KC_HOSTNAME_PORT` | **Weg** (`:78-79`) | Verwijderd in 26.0: "There is no way to set path or port separately", dat gaat nu via de volledige URL. Een verwijderde optie die als `KC_`-variabele blijft staan, is een startfout en niet een waarschuwing; ga dat niet uitproberen op productie |
| `KC_PROXY_ADDRESS_FORWARDING` | **Weg** (`:80-81`) | De `proxy`-optie en zijn varianten zijn in 26.0 verwijderd, vervangen door `proxy-headers` plus de hostname-opties |
| `KC_PROXY_HEADERS` / `--proxy-headers` | Eén waarde, op één plek, en dat wordt `xforwarded` in de env. De arg vervalt | `xforwarded` is wat nginx en HAProxy sturen. Vandaag wint de arg met `forwarded` en ziet Keycloak het proxy-IP in plaats van dat van de bezoeker. Dit is een beveiligingsrelevant verschil (events, brute-force) en geen opmaakkwestie |
| `KC_HOSTNAME_STRICT` / `--hostname-strict` | Eén plek, de env (`:76-77`). De arg vervalt | Nu staan ze allebei op `false`. Twee keer hetzelfde is geen fout, maar het is wel waar de volgende afwijking onopgemerkt in gaat zitten |
| `KC_HEALTH_ENABLED`, `KC_METRICS_ENABLED`, `KC_DB`, `KC_FEATURES` | Alleen in de Dockerfile, niet ook in de env | Alle vier build-time. Dit is exact de oorzaak van de augmentatie bij elke start, en het is de beslissing van de RC-212-rework |
| `KC_HTTP_ENABLED` | Blijft (`:72-73`) | Runtime, en TLS eindigt op de ingress |
| `KC_DB_URL`, `KC_DB_USERNAME`, `KC_DB_PASSWORD` | Blijven ongewijzigd | Runtime, en ze veranderen niet in 26 |
| `KC_SPI_ADMIN_REALM_RESTAPI_EXTENSION_ENABLED` | **Te onderzoeken, en dan of weg of in de juiste vorm** (`:98-99`) | Onze twee endpoints zitten op `realm-restapi-extension`, niet op `admin-realm-restapi-extension`, en niets in deze repo leest deze variabele. In de dubbelstreepvorm zou hij trouwens dubbele underscores nodig hebben. Meet of de endpoints het zonder hem ook doen; doen ze dat, dan hoort hij weg |
| `KEYCLOAK_ADMIN`, `KEYCLOAK_ADMIN_PASSWORD` | Omzetten naar `KC_BOOTSTRAP_ADMIN_USERNAME` en `KC_BOOTSTRAP_ADMIN_PASSWORD` (`:56-65`), sleutelnamen in het geheim meeverhuizen | Gedeprecieerd in 26.0, niet verwijderd. Doen, maar reken er niet op dat een probleem hier zit |
| `ORGANIZATION`, `KEYCLOAK_ADMIN_IMPORT_ENABLED`, `KEYCLOAK_ADMIN_EXPORT_ENABLED` | Noteren, niet stilzwijgend meenemen (`:96-97`, `:102-105`) | Geen enkele plek in deze repo leest ze en het zijn geen Keycloak-opties. Ze doen vandaag niets en op 26 ook niet. Opruimen is een eigen wijziging; bij een env-audit horen ze wel benoemd te worden |
| `ZAD_MAIL_RELAY_*` | Onveranderd | Onze eigen variabelen, gelezen door onze eigen provider |
| `readinessProbe` op `/realms/master:8080` (`:158-161`) | Mag naar `/health/ready` op 9000, als eigen stap | In 26 is de management-interface op 9000 de bedoelde plek en `health-enabled` staat al aan. Het is een verbetering en geen voorwaarde: doe het los, zodat een falende probe tijdens de upgrade niet twee oorzaken kan hebben |

## Van 25.0.6 naar 26.7.4: één sprong, met de pod uit

**Eén sprong, geen tussenversie.** De upgrading guide van Keycloak is cumulatief: hij beschrijft per minor wat er verandert tussen jouw versie en je doel, en schrijft geen tussenstop voor. Wat wél hard in die guide staat, is de volgorde rond de database: "Before you migrate the database, shut down all Keycloak nodes running the old version", en "rolling updates are not possible for minor or major upgrades". Ik heb geen upstreamzin gevonden die letterlijk zegt dat de schemamigratie onomkeerbaar is; behandel hem zo, want dat is wat de guide impliceert en wat elke restore-oefening hierna aanneemt.

Dat vertaalt zich in één concrete wijziging die vandaag ontbreekt: **`strategy: type: Recreate` op de Keycloak-deployment.** Er staat geen `strategy` in `base/deployment.yaml`, dus de standaard RollingUpdate start de 26-pod terwijl de 25-pod nog draait, en dan migreert de nieuwe pod het schema onder de oude door. Dat is precies wat upstream verbiedt. Het patroon staat al in deze repo, bijvoorbeeld `infrastructure/bootstrap/infrastructure/redis/controller/base/deployment.yaml:12`.

De migratie zelf gebeurt automatisch bij de eerste start van de 26-pod. Twee dingen die dat langer maken dan een gewone herstart: de schemamigratie over de sessie- en eventtabellen, en de augmentatie die vandaag nog bij elke start gebeurt. Reken op minuten, niet op seconden, en pin geen krappe `initialDelaySeconds` erop.

## Volgorde

Twee voorwaarden vóór stap 1, en ze staan er niet voor de vorm:

- **De vlagverdeling van RC-212 is vervuld**: de vier bouwopties staan alleen in de Dockerfile en de pod start met `--optimized`. Zonder dat zou je op 26 een pod meten die bij elke start opnieuw augmenteert, en was elke afwijking aan twee dingen tegelijk toe te schrijven.
- **RC-212 gaat eerst naar productie, op 25.0.6.** De eigen image heeft daar nog nooit gedraaid. Ga je in één keer naar de eigen image én naar 26, dan is een storing niet toe te wijzen. Dat is dezelfde regel die `features/futures/keycloak-26-upgrade.md` al stelde voor het logout-endpoint.

Daarna:

1. `keycloak.version` op `26.7.4` in `keycloak-migration/relay-email-sender/pom.xml:19` en `keycloak-migration/custom-mapper/pom.xml:19`, en dezelfde versie in `images/keycloak-zad/Dockerfile:25`. Toets: `test_de_basisversie_is_die_waar_de_modules_tegen_bouwen` blijft groen, en die toets is de reden dat dit niet half kan.
2. Artefactversies verhogen (custom-mapper naar 1.2.0, relay naar 1.1.0), met bij de relay `RelayMailConfig.VERSION` mee (`RelayVersieTest` pint die twee aan elkaar) en de echo's in de taken `build-keycloak-custom-mapper` en `build-keycloak-relay-email-sender`, die de jarnaam uitschrijven.
3. `mvn -B package` per module, via `task test-keycloak-custom-mapper` en `task test-keycloak-relay-email-sender`. CI draait dit bij elke pull request (job `keycloak-providers` in `.github/workflows/ci.yml`). Repareer wat omvalt, en lees de tabel hierboven: dat een module compileert, betekent bij drie van de zes providers niets.
4. De optie-opruiming uit de vorige paragraaf, inclusief `strategy: Recreate` en de overlaypatches.
5. `task publish-keycloak-image`. Die bouwt, pusht naar ghcr, controleert of de tag anoniem te halen is en pint alleen de sandbox-overlay. Faalt `kc.sh build` op het provider-id, dan zingt de canarie hier en niet in een pod.
6. `task sandbox:sync`, en daarna de volledige toetsenlijst uit "Klaar als".
7. De dump-en-restore-oefening uit "Terugval", op de sandbox, vóór productie. Niet erna.
8. `task pin-keycloak-image TARGET=base`: dezelfde tag, geen herbouw. De taak weigert een `-dirty-`tag en leest de tag uit de sandbox-overlay, dus met de hand een versienummer invullen kan hier niet. Commit, ArgoCD synct, ODCN erft de tag uit de basis.
9. Op productie: de pg_dump uit "Terugval" vóór de sync, dan de rollout volgen, dan dezelfde toetsenlijst.

## Terugval

De database heeft **geen backup**. `infrastructure/bootstrap/infrastructure/postgresql/database/base/cluster.yaml:14` draait één instance en het `backup`-blok op `:101-115` staat uitgecommentarieerd. De terugvalweg bestaat dus alleen als iemand hem vooraf maakt.

1. **Vooraf, op productie: `pg_dump` van de database `keycloak` op `rig-db`**, weggeschreven buiten het cluster, met het tijdstip en de draaiende image-tag in dezelfde notitie. Zonder dit is er geen terugval, alleen een voorwaartse reparatie.
2. **De restore is geoefend voordat productie omgaat**, op de sandbox: dump van de sandbox-Keycloak, restore in een lege database, 25.0.6 ertegenaan, inloggen. Een ongeoefende restore is een aanname.
3. **De tag om naar terug te gaan** is de waarde die in `base/deployment.yaml` stond vóór de promotie. Hij is niet uit het hoofd te weten en hoeft dat ook niet: `git log -p` op dat bestand geeft hem, en de terugval is het terugdraaien van die ene commit.
4. **De image alleen terugzetten werkt niet.** 25.0.6 leest het 26-schema niet. Een terugval is dus altijd: pod uit, database terug naar de dump, tag terug, pod aan. Alles wat tussen de dump en de terugval in Keycloak is gebeurd, is dan weg.
5. **Wat "weg" betekent, verschilt per soort.** Realmconfiguratie kan OPI opnieuw neerzetten uit de blueprints. Nieuwe gebruikers, gekoppelde identiteiten, wachtwoordwijzigingen en sessies kan niemand terugzetten. Houd het venster tussen dump en akkoord daarom kort, en zeg vooraf wie beslist dat we terugvallen.

## Valkuilen

**Een gedeprecieerde API is een stille wijziging.** `getIdentityProviderByAlias` en `AuthResult.getSession()` compileren op 26.7.4 gewoon door. De plek die in 26.0 is verbouwd, is precies de plek waar de eerste van die twee naar kijkt.

**De metriekenexport faalt soft en liegt dan.** Breekt de JPQL, dan blijft het antwoord 200 en verschuift elke gebruiker naar `idp="local"`. Prometheus scrapet dit endpoint in beide omgevingen (`infrastructure/bootstrap/infrastructure/prometheus/controller/base/configmap.yaml:127-150` en de odcn-patch op `:100-120`), dus een target dat "up" is, bewijst niets over de inhoud.

**`task test-keycloak-custom-mapper-docker` bewijst vandaag niets.** `test-local-docker.sh:9` en `:28` verwijzen naar `keycloak-saml-nameid-mapper-1.0.0.jar` terwijl de pom op 1.1.0 staat, dus docker mount een pad dat niet bestaat, en de "controle" op `:61-65` meldt bij afwezigheid van de mapper dat dat normaal is. Het script draait bovendien `start-dev` op `quay.io/keycloak/keycloak:26.0.0` (`:29`), wat op het eerste gezicht lijkt op een 26-toets die al groen staat. Repareer het of gebruik het niet, maar haal er geen bewijs uit.

**Het gaat om interne SPI's, en dat betekent per patchversie.** Keycloak waarschuwt bij alle zes providers dat de SPI zonder aankondiging kan wijzigen. Deze migratie is dus geen eenmalige klus: elke volgende basisversie vraagt dezelfde toetsenlijst, en dat is een argument om die lijst als toets vast te leggen in plaats van in een plan.

**Twee dingen in 26.7 raken de admin-API waar OPI op leunt.** De alias van een identity provider is niet meer te wijzigen via de REST-API, en de rol `view-system` is verdwenen (server-info vraagt nu `manage-realm` in het master-realm). OPI werkt IdP's bij met de bestaande alias in de payload (de 409-conflicttak van `add_identity_provider` in `opi/connectors/keycloak.py`, die met `update_idp` bijwerkt), dus dit hoort goed te gaan; een reprocess van een bestaand project op de sandbox is de meting die het zegt. Verder zijn identity providers sinds 26.0 niet meer opgenomen in de realmrepresentatie: OPI zet ze via de eigen IdP-weg, maar alles wat een realm UITLEEST en daar IdP's in verwacht, ziet ze niet meer.

**`adminEventsExpiration` is geen veld maar een attribuut.** De vervolgstap in `features/keycloak-26-upgrade.md:153-160` gaat uit van een veld naast `eventsExpiration`. In 26 staat de bewaartermijn voor admin-events in de `attributes`-map van de realm, en dat is nog een open punt bij Keycloak zelf. Niet in deze migratie, maar het staat verkeerd in dat document.

## Wat we niet doen

- **Niet naar 26.8 of verder.** Doel is 26.7.4, de nieuwste patch van de enige onderhouden lijn op vandaag.
- **Geen realmherinrichting.** De blueprints in `opi/configs/keycloak/*.yaml` blijven zoals ze zijn, tenzij 26 een veld afdwingt. `adminEventsExpiration` valt hierbuiten.
- **Geen themawerk buiten compatibiliteit.** Wel de keuze uit de themaparagraaf, niet een eigen thema of een migratie naar `keycloak.v2` als structuur.
- **Geen oplossing voor de augmentatie bij elke podstart.** Dat is de RC-212-rework; deze migratie hangt eraan maar repareert hem niet.
- **Geen backupvoorziening voor `rig-db`.** Wat hier gebeurt is één dump met de hand. Dat de platformdatabase geen backup heeft, is een eigen taak en een grotere dan deze.
- **Geen tweede image en geen andere distributie.** De eigen image blijft de enige weg.
- **De readinessProbe naar 9000 verhuizen** mag, maar niet in dezelfde stap als de versiesprong.

## Klaar als

Elk criterium met hoe je het meet. De eerste vier bewijzen de providers; dat de pod opkomt is het begin en niet het bewijs.

1. **De zes providers doen op 26.7.4 hun werk, elk aangetoond met de toets uit de providertabel.** Meten: een SSO-Rijk-login die het NameID-attribuut zet, een logout waarna de sessie uit de admin-API weg is, een `rig_keycloak_users_by_idp_total`-regel met een echte alias erin, een login die de teller verhoogt, een gebruiker zonder rol die de gestileerde 403 krijgt en een mét rol die binnenkomt, en een bevestigingsmail met het merkteken `X-ZAD-Email-Sender` erop. Zes uitkomsten, in de PR.
2. **De emailSender-aanvalsproef en de tegenproef zijn opnieuw gedaan op 26.7.4.** Meten: zoals `features/keycloak-26-upgrade.md:51-63` het voorschrijft. Een lege log zonder tegenproef is geen bewijs.
3. **De pod augmenteert niet bij het starten.** Meten: "Changes detected in configuration" staat niet in de startlog. Dit criterium is geërfd van de RC-212-rework en staat hier omdat de opties in deze migratie opnieuw worden aangeraakt.
4. **De startlog bevat geen enkele waarschuwing over een onbekende of verwijderde optie, en de pod is niet eerder gefaald op een startfout.** Meten: de volledige startlog van een verse pod in de PR.
5. **Het IP van de bezoeker staat in de events, niet dat van de proxy.** Meten: één login op de sandbox, dan het login-event in het admin-console of via de admin-API, en het IP vergelijken met het eigen adres.
6. **De overlays bouwen en de guards staan er nog.** Meten: `kustomize build` op elk overlaypad, en na de promotie `grep` op de tag in basis en overlay.
7. **De restore is geoefend, niet alleen beschreven.** Meten: het verslag van de sandbox-restore in de PR, met de commandoregel die hem terugzette en een inlog erna.
8. **Prometheus scrapet Keycloak nog en levert inhoud.** Meten: het target van job `keycloak-rig-metrics` staat up EN `rig_keycloak_users_by_idp_total` heeft meer dan één alias.
9. **OPI verwerkt een bestaand project opnieuw zonder fout op de admin-API.** Meten: een reprocess op de sandbox, en de log zonder 4xx van Keycloak.
10. **Het inlogscherm, de accountpagina's en de foutpagina van de rolpoort renderen.** Meten: drie schermafbeeldingen, of de keuze uit de themaparagraaf expliciet vastgelegd als er nog geen 26-thema is.

## Open vragen voor de opdrachtgever

1. **Heeft MinBZK/keycloak-theme een release die 26 ondersteunt?** Zo niet: wachten, zelf voorstellen, of doorzetten met een gewijzigd inlogscherm? Dit is de enige vraag die de migratie kan blokkeren.
2. **Gaat RC-212 eerst naar productie op 25.0.6?** Dat is mijn voorstel omdat het een storing toewijsbaar maakt, maar het kost een tweede promotieronde en een tweede moment waarop Keycloak even uit is.
3. **Wie beslist over terugvallen, en hoe lang mag het venster tussen dump en akkoord zijn?** Daarbinnen is elke nieuwe gebruiker en elke gekoppelde identiteit onherstelbaar.
4. **Mogen de drie dode variabelen (`ORGANIZATION`, `KEYCLOAK_ADMIN_IMPORT_ENABLED`, `KEYCLOAK_ADMIN_EXPORT_ENABLED`) in deze wijziging weg,** of blijven ze staan omdat ze buiten de opdracht vallen?
5. **Wordt de optie-opruiming één commit met de versiesprong, of een eigen commit ervoor?** Apart is beter uit te leggen en levert twee sandboxrondes op.
