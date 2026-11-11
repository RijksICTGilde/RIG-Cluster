# Keycloak 26.8.1: het runbook voor de versiesprong

De gedeelde Keycloak van het platform gaat van 25.0.6 naar **26.8.1** (13 oktober 2026).
Keycloak onderhoudt alleen de laatste minor, dus 25.0 krijgt sinds oktober 2024 geen fixes
meer en ook de 26.7-lijn niet.

De eis is **behoud van alle data**. Realms, clients, gebruikers, wachtwoord-hashes,
OTP-secrets en gekoppelde identiteiten staan alleen in de database `keycloak` op `rig-db`, en
de schemamigratie van Keycloak is eenrichtingsverkeer. Dit document is de weg daarnaartoe:
wat er in de code klaarstaat, wat vooraf groen moet zijn, wat er in het venster gebeurt, en
wanneer en hoe er teruggevallen wordt.

## Wat er in git staat (RC-212, RC-411, RC-412)

- een eigen image met de providers en het thema erin, gestart met `--optimized`
  (`features/keycloak-eigen-image.md`);
- `strategy: Recreate`, dus de schemamigratie loopt nooit met twee pods;
- de basisversie op `26.8.1` in `images/keycloak-zad/Dockerfile`, met `keycloak.version` in
  beide poms gelijk daaraan (een toets pint dat);
- de opstartbeheerder heet `KC_BOOTSTRAP_ADMIN_USERNAME` / `KC_BOOTSTRAP_ADMIN_PASSWORD`;
- `task test-keycloak` draait de hele groep `keycloak_live` tegen de gepinde image: per
  provider registratie EN bereikbaarheid, de mailketen tegen twee SMTP-sinks, de idp-telling,
  de inlogpagina met het thema, de admin-API van OPI en de scenario's over meerdere
  deployments in een realm (`task test-keycloak-image` doet hetzelfde tegen een zelf gebouwde
  image);
- `task keycloak-telling` geeft de aantallen waarmee behoud van data te bewijzen is;
- `task keycloak-proefmigratie` migreert een KOPIE van de database en vergelijkt die telling
  voor en na.

## Wat er op de API van 26.8.1 brak, en hoe het is opgelost

Gemeten door beide modules tegen `org.keycloak:*:26.8.1` te bouwen (RC-412).

| Wat | Hoe het nu staat |
|---|---|
| `EmailSenderProvider.validate(Map)` is verplicht geworden | Onze verzender keurt alles goed. Keycloak roept hem bij elke realm-create en -update aan (`SMTPUtil.checkSMTPConfiguration`); gooien zou elke realmwijziging van OPI laten falen op een configuratie die daarna toch genegeerd wordt. |
| `EmailException(Exception)` is weg | Een `EmailException` draagt nu een tekst, en die komt in de beheerconsole; dus zonder host, adres of foutdetail. |
| De `@Provider`-workaround op de twee endpoints | Weg. In de bron van 26.8.1 draagt die annotatie alleen ROOT-resources (`RealmsResource`, `AdminRoot`); een klasse die uit een sub-resource locator komt heeft hem niet, zoals Keycloaks eigen `DeviceEndpoint`. |
| `FederatedIdentityEntity` (private JPA-API) | Entiteit en velden heten in 26.8.1 nog hetzelfde. Gemeten, niet aangenomen: `test_rig_metrics_telt_een_gekoppelde_gebruiker_per_idp` maakt een echte koppeling en leest de telling terug. |
| `IdpEmailVerificationAuthenticator` | Grendelt nog op `realm.getSmtpConfig().isEmpty()`. De minimale `smtpServer` op elke realm blijft dus doen wat hij deed; zie `features/keycloak-mail.md`. |
| `--features=admin-api` | `ADMIN_API` bestaat nog als default-feature. |
| De enkele-streep spi-vorm (`--spi-email-sender-provider=`, `--spi-theme-welcome-theme=`) | Werkt nog. 26 kent een nieuwe vorm met een dubbele streep, maar de oude wordt nog gelezen: `PropertyMappers.isSpiBuildTimeProperty` doet dat met opzet niet strikt ("we can't require the new property format until we're ok with a breaking change"), en `MicroProfileScope.getValue` leest beide namen. De args blijven daarom staan zoals ze staan. |
| De alias van een authenticator config moet uniek zijn in het realm | Aanmaken met een bestaande alias geeft een 409; 25.0.6 accepteerde dat. De alias volgt nu de flow in plaats van de rol of de provider, want deployments van een project delen een realm. Configs van vóór deze wijziging krijgen hun nieuwe alias bij de volgende reconcile. |
| STARTTLS op de standaardverzender | Strenger in 26.8.1: met `starttls` aan zet Keycloak ook `starttls.required`. Ons pad raakt dat niet, en `task test-keycloak-image` meet dat met `starttls: "true"` in de proefrealm. |

De mail-SPI is een **interne** SPI en draagt een veiligheidseigenschap: zolang `zad-relay`
verstuurt, leest Keycloak de `smtpServer` van geen realm en kan een projectbeheerder de post
niet omleiden. Valt die provider stil weg, dan verstuurt de standaardprovider **zonder
foutmelding**. Daarom zijn vier metingen nu gebakken in een toets: relay-verzending
(bericht via onze relay, niet via `smtpServer` van de realm), de fallback van de standaard-provider
(zonder relay draait mail gewoon door), idp-telling op `/rig-metrics` met een echte federative-identity,
en het thema rendering, met een tegenproef die in `features/keycloak-mail.md` staat.

## Vooraf, en dit moet groen zijn

Alles hieronder gaat over de tag die gepromoveerd gaat worden, niet over een herbouw: een
herbouw levert een ander artefact op en dan is het bewijs van de sandbox niets waard.

| Stap | Wie | Groen is |
|---|---|---|
| `task test-keycloak IMAGE=<de te promoveren tag>` | bouwer of beheerder, lokaal met Docker | de hele groep `keycloak_live` groen op die tag: providers, mail, thema, admin-API en scenario's. **Verplichte stap bij elke versiesprong, voor promotie.** Admin-API (`tests/integration/test_keycloak_admin_api.py`, RC-413): de enige meting van de API waar OPI elk project-realm mee inricht. Scenario's (`tests/integration/test_keycloak_scenarios.py`, RC-433): de enige meting van meerdere deployments in EEN realm, en dus van wat 26.8.1 brak. Een versiesprong die alleen de losse methoden meet laat precies dat door. |
| de tegenproef van de mailtoets | dezelfde | zonder de vlag landt het bericht WEL op de lokaas-sink; zie `features/keycloak-mail.md` |
| `uv run pytest tests/e2e/test_keycloak_logout_harness.py -m keycloak_harness` | bouwer of beheerder, lokaal met Docker en Maven | de negen toetsen van de uitlogketen slagen; dat harnas start zelf een Keycloak 26.8.1 met de shim |
| `task keycloak-proefmigratie IMAGE=<de te promoveren tag>` op de sandbox | beheerder | `diff` van de telling is leeg, en de beheerder kan op de kopie inloggen. Dit is de ENIGE meting van de 25-naar-26 schemamigratie: `task test-keycloak-image` draait op een lege database en zegt daar niets over. Zonder `IMAGE` pakt de task de tag uit de sandbox-overlay, en die kan nog 25.0.6 zijn |
| de sandbox zelf | beheerder | de checklist hieronder |
| `task keycloak-proefmigratie` op productie | **besluit van de eigenaar** | zie "Buiten dit runbook" voor het besluit en de variabelen die ODCN nodig heeft |

### De checklist op de sandbox

- SSO Rijk-login op `rig-platform` (de XPath-mapper en de FORCE-attribuutmappers);
- logout via `always-clear-session-logout`;
- `GET /realms/master/rig-metrics` met het token: metrieken, en de foutgauge op `0`;
- client access restriction (`RequireClientRoleAuthenticator`);
- een TWEEDE deployment met `restrict-access` in een BESTAAND realm, en daarna een
  reprocess van allebei: dat is de stap waarop de 409 van 26.8.1 viel, en een eerste
  deployment zegt daar niets over;
- een template-wissel op dat realm (`sso-support` naar `sso-only` en terug), met daarna een
  inlog op elk van de twee deployments;
- de inlog- en accountpagina's renderen met `nl-design-system` (`keycloak-nldd-thema.md`);
- de opstartbootstrap van OPI loopt schoon (realm create/update, met de audit-instellingen);
- de invite-flow en een realm uit de wizard;
- de vier mailmetingen, met de tegenproef. Een bevestigingsmail die aankomt is NIET genoeg:
  kijk of het merkteken `X-ZAD-Email-Sender` erop staat.

## Het venster

Eerst op staging, met hetzelfde venster: `task pin-keycloak-image TARGET=staging` in plaats
van `TARGET=base` in stap 3, en de namespace `rig-staging-operations`. Productie blijft dan op
de basis; de promotie naar de basis haalt de staging-pin daarna weer weg.

1. **De nulmeting.** `task keycloak-telling CONTEXT=prd NAMESPACE=rig-prd-operations OUT=voor.txt`.
   Dat zijn realms, clients, gebruikers, credentials per type en gekoppelde identiteiten.
   Realmconfiguratie kan OPI hergenereren, gebruikers en koppelingen niet.
2. **Een verse dump, die in het cluster blijft.** De nachtelijke platform-back-up van RC-278
   (`rig-db-platform-backup`), of een handmatige run daarvan. Dit is de terugvalweg, want de
   schemamigratie is niet omkeerbaar.
3. **Promoveren.** `task pin-keycloak-image TARGET=base`: dat verplaatst precies de tag die op
   de sandbox heeft gedraaid naar de basis, waar ODCN hem uit erft. Commit, en laat ArgoCD
   syncen.
4. **Wachten tot de pod ready is.** `strategy: Recreate`, dus de oude pod is uit voordat de
   nieuwe begint: een korte onderbreking, en nooit twee pods op hetzelfde schema. De
   schemamigratie loopt in de opstartlog van de nieuwe pod.
5. **De controlemeting.** `task keycloak-telling ... OUT=na.txt` en `diff voor.txt na.txt`.
6. **De drie controles op de werkende voorziening:** een SSO Rijk-login op `rig-platform`, het
   logout-endpoint, en `/realms/master/rig-metrics` met de foutgauge op `0`.

## Terugvallen

**Wanneer:** elk verschil in de telling, of een login die faalt. Dat zijn geen details.

**Wie beslist:** de eigenaar van het platform. De beheerder die het venster draait meldt het
verschil en valt niet op eigen initiatief terug.

**Hoe:** de vorige tag terugzetten in de basis EN de dump terugzetten in een LEGE database
`keycloak`. Beide, niet een van de twee: de oude versie kan niet op het nieuwe schema. Met
`strategy: Recreate` draait er bij die terugval nooit een oude pod op het nieuwe schema.

## Na afloop

- `adminEventsExpiration` toevoegen aan de realm-blauwdrukken
  (`operations-manager/python/opi/configs/keycloak/*.yaml`) en aan het `create_realm`-pad van
  de connector, zoals `eventsExpiration` al gaat. 25 had geen ondersteunde verloopdatum voor
  adminevents, 26 wel, en OPI maakt er bij elke reconcile.
- Een paar weken later de omvang van `admin_event_entity` en `event_entity` nakijken en de
  bewaartermijn daarop zetten.
- Persistente gebruikerssessies zijn vanaf 26 de standaard: sessies overleven een podherstart,
  ten koste van groei in de sessietabellen.

## Buiten dit runbook

- **De productiewissel zelf** is het venster hierboven, met een mens erbij.
- **`task keycloak-proefmigratie` op productie draaien** vraagt een besluit van de eigenaar:
  die task zet tijdelijk resources in de productienamespace met `kubectl apply`, en productie
  verandert normaal alleen via Git en ArgoCD. De task maakt het mogelijk, hij beslist het niet.
  Valt dat besluit ja, dan moet ODCN zijn eigen beeld, opslag en pull-secrets mee krijgen:

  ```bash
  task keycloak-proefmigratie CONTEXT=prd NAMESPACE=rig-prd-operations \
    IMAGE=rcr.rijksapps.nl/ghcr-rig/rijksictgilde/zad/keycloak-zad:<tag> \
    PG_IMAGE=rcr.rijksapps.nl/ghcr-rig/cloudnative-pg/postgresql:17.11 \
    STORAGE_CLASS=ocs-storagecluster-ceph-rbd \
    PULL_SECRET=rig-robot-pull-secret,ghcr-rig-robot-pull-secret
  ```

  Zonder `PG_IMAGE` op het rcr-pad herschrijft admission `imageName` en vervangt CNPG zijn
  eigen pod in een lus (gemeten op 1 september bij `rig-db`), zonder `STORAGE_CLASS` komt er
  geen volume, en zonder de pull-secrets faalt de pull op de rcr-proxy.
- **Een nieuw NLDD-thema** (#189), **inloggen via e-mail** (#262).
- **Back-ups van `rig-db` in productie** (#203) zijn een voorwaarde voor het venster, geen
  onderdeel van deze sprong.

## Afhankelijkheden

- `images/keycloak-zad/extensions/custom-mapper/` en `images/keycloak-zad/extensions/relay-email-sender/` (de jars)
- `keycloak-migration/proefmigratie/` (de manifesten van de proef)
- `images/keycloak-zad/theme/` (het loginthema, sinds RC-424 in deze repo; `keycloak-nldd-thema.md`)
- de realm-blauwdrukken: `operations-manager/python/opi/configs/keycloak/*.yaml`
- de connector: `operations-manager/python/opi/connectors/keycloak.py` (`create_realm`)
- de overlays: `infrastructure/bootstrap/infrastructure/keycloak/controller/overlays/`
