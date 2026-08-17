# Zelfbediening op identiteit dichtzetten in projectrealms (IdP koppelen/ontkoppelen en wachtwoord zetten)

Status: fase 1 gebouwd (2026-08-01), nog niet uitgerold. Aanleiding: een gebruiker kan in de account console van een projectrealm zelf een wachtwoord zetten en vervolgens de koppeling met de `rig-platform-oidc` IdP verwijderen. Vanaf dat moment logt die persoon lokaal in, zonder SSO Rijk en zonder de platform-realm. Praktisch getest en werkend bevonden.

## Besluit

Beide knoppen gaan dicht op elke projectrealm met een identity provider, dus knop 1 en knop 2 uit de analyse hieronder. Niet knop 3 (de account-feature serverbreed uit) en niet smaak B (per template splitsen).

De afweging die daaronder ligt, en die afwijkt van wat deze notitie eerst adviseerde:

- **Smaak B is afgevallen** omdat de eis per gebruiker is geformuleerd ("een wachtwoord zetten terwijl je al een IdP hebt is raar") en per realm splitsen dat niet levert. Op `sso-support` zou de omzeiling gewoon blijven bestaan, en dat is de default van het template-veld, dus de meerderheid van de projecten.
- **Smaak C (eigen required-action provider) is afgevallen** omdat de prijs van knop 2 in onze praktijk vrijwel nul blijkt. Dat is de correctie op de alinea "De prijs, en dat is een productbeslissing" hieronder: `create_user()` zet het wachtwoord hardcoded met `temporary: False` en zet nergens `requiredActions`, dus wij gebruiken de flow die knop 2 zou breken helemaal niet. Lokale gebruikers kiezen hun wachtwoord in óns invite-formulier, dat via de admin-API schrijft en `UPDATE_PASSWORD` nergens raakt. Zelf wijzigen kon alleen via een account console waar geen enkele link naartoe wijst, en "wachtwoord vergeten" kon al niet omdat `resetPasswordAllowed` hardgecodeerd op `False` staat en er geen SMTP is. Er gaat dus geen functie verloren die vandaag werkt.
- **Knop 3 is niet nodig** zodra knop 1 en 2 dicht zijn, en is serverbreed waar deze per realm kan.

Wat wél nog moet gebeuren is de vervanging: echte wachtwoord-zelfbediening voor lokale accounts, buiten Keycloak om. Dat is fase 2 en die wacht op de mailserver. Zie onderaan.

## Fase 1: wat er gebouwd is

- `KeycloakConnector.set_required_action_enabled(realm, alias, enabled)` en `KeycloakConnector.remove_default_role(realm, client_id, role_name)` in `opi/connectors/keycloak.py`, naast `_lock_identity_fields()` en met hetzelfde patroon: idempotent, alleen schrijven bij een echt verschil, en fail closed omdat een realm die deze restricties mist in de kwetsbare configuratie draait.
- Twee sleutels op het `realms:`-item in de blueprints, verwerkt door `KeycloakYamlHandler._apply_realm_self_service()`:

  ```yaml
  disabledRequiredActions:
    - UPDATE_PASSWORD
  removeFromDefaultRoles:
    - account:manage-account
  ```

  De naam is `removeFromDefaultRoles` en niet `removeDefaultRoles`, want dat laatste bestaat al als vlag op een *user*-item in `algoritmeregister.yaml`.
- Gezet in `sso-only.yaml` en `sso-support.yaml`. **Niet** in `algoritmeregister.yaml`: die realm heeft geen enkele identity provider ("local authentication only"), dus daar is geen SSO om te omzeilen en zouden de restricties alleen kosten met zich meebrengen. Ook niet in de `bootstrap*.yaml` van de platform-realm, zie de openstaande vraag onderaan.
- Toegepast op de create-weg (`_process_realms()`) én op de reconcile-weg (`KeycloakManager._ensure_realm_self_service()` naast de andere `_ensure_*`). Dat tweede is essentieel: `create_realm()` draait alleen bij een nieuwe realm, en dat is precies waarom `_lock_identity_fields()` vandaag geen enkele bestaande realm heeft geraakt.
- Alleen déze twee sleutels worden uit de blueprint gelezen. De rest van het plumbing-gat hieronder staat nog open en is bewust niet meegenomen, zodat het een zichtbare aparte beslissing blijft.
- `scripts/keycloak_self_service_report.py`: pre-flight rapport, alleen lezen. Draaien vóór uitrol.
- Tests: `tests/test_keycloak_self_service.py`.

## Wat er in de sandbox gemeten is (1 augustus)

Getest op `vlam-wt8-sandboxed-local`, een `sso-support`-realm met de `rig-platform-oidc` IdP en één federated gebruiker.

**De omzeiling is eerst end-to-end gereproduceerd.** Met een via de admin-API gezet wachtwoord logt de federated gebruiker lokaal in, zonder SSO Rijk. De handmatig samengestelde `kc_action=UPDATE_PASSWORD`-URL serveerde het wachtwoordformulier, en `DELETE /account/linked-accounts/rig-platform-oidc` gaf **204**: de IdP-koppeling was daadwerkelijk weg. Dit is dus geen theoretisch gat.

**De scharnierende aanname klopt.** Met `UPDATE_PASSWORD` op `enabled: false` negeert Keycloak de `kc_action`-parameter volledig: de gebruiker wordt direct doorgestuurd naar de redirect-URI, zonder wachtwoordscherm. De AIA-route is daarmee dicht, en dat was de reden dat knop 3 alleen niet genoeg zou zijn.

**Eén aanname klopte níet, en dat is een correctie op de analyse hierboven.** De verwachting was dat de account console read-only zou blijven omdat `view-profile` behouden blijft. Dat is niet wat er gebeurt. Zonder `manage-account` bevat het access token *helemaal geen* account-rollen meer, dus ook niet de `account`-audience, en antwoorden álle account-REST-endpoints met 401: `credentials`, `linked-accounts`, de unlink en de profielupdate. De console is inert, niet read-only. Een controlemeting bevestigt de richting: met `manage-account` terug in het composiet komen dezelfde vier calls op 200/204/204 en lukt de unlink weer. Voor ZAD is dat geen bezwaar, want er verwijst niets in de portal naar de account console, maar het is meer dan de analyse suggereerde.

**Beide connector-methodes zijn idempotent gebleken tegen een live Keycloak**: de tweede aanroep schrijft niets en logt niets.

**De reconcile-weg is via de echte API bewezen**, op twee realms: `vlam-wt8-sandboxed-local` (`sso-support`) en `tas-yz7-sandboxed-local` (`sso-only`). Een realm die handmatig in de open toestand was teruggezet, kreeg beide restricties terug na een gewone `GET /api/projects/<naam>/:refresh`, met de twee logregels als bewijs. Een tweede refresh schreef nul regels.

**De create-weg is ook bewezen**, met een wegwerprealm uit elk van beide blueprints: `_process_realms()` levert direct bij aanmaken `UPDATE_PASSWORD=false` en een composiet zonder `manage-account`.

**Het rapportscript is op beide categorieën geraakt**, niet alleen op een lege uitkomst: een gebruiker met een openstaande `UPDATE_PASSWORD` en een federated gebruiker met een wachtwoord-credential werden allebei gemeld, met exit code 1.

### De lokale flows blijven werken

Dit is de vraag waar het besluit op rust, dus apart gemeten op realms waar de restricties actief zijn.

- **Aanmaken werkt.** `create_user()` met dezelfde argumenten als de invite-flow (`invite_manager.complete_local_invite`) levert een gebruiker met een `password`-credential en een lege `requiredActions`. De uitgezette required action zit die weg niet in de weg, precies omdat de invite-flow via de admin-API schrijft.
- **Inloggen werkt.** Diezelfde lokale gebruiker logt daarna gewoon in via de browser-flow en krijgt een token. Ook in de strakke A/B hieronder levert de gewone lokale login in de dichte toestand nog steeds een authorization code op.
- **Niet nagespeeld**: de HTML-kant van de invite-flow (formulier, CSRF, opzoeken van de invite in het projectbestand). Die raakt Keycloak niet, dus mijn wijziging kan hem niet breken; wat hij wél raakt, `create_user` en de login erna, is hierboven echt getest.

### Op `sso-only` is een lokaal wachtwoord sowieso waardeloos

Gemeten op `tas-yz7-sandboxed-local`, een echte `sso-only`-realm met de External IDP Redirector. Een lokale gebruiker met een geldig wachtwoord krijgt daar **geen inlogformulier**: de browser-flow stuurt onmiddellijk door naar `broker/rig-platform-oidc/login`, vandaar naar `rig-platform`, naar `broker/sso-rijk/login` en verder de federatieketen in. Er is dus geen lokaal inlogscherm om een wachtwoord aan aan te bieden.

### Hertoets

De eerste ronde liep over verschillende realms en toestanden door elkaar. Daarom overgedaan op één wegwerprealm met één federated gebruiker met wachtwoord, waarbij per meting alleen de knoppen omgingen. Alle vier de claims houden stand in beide richtingen:

| | open | dicht |
|---|---|---|
| `kc_action=UPDATE_PASSWORD` | wachtwoordpagina geserveerd | genegeerd, direct naar redirect-URI |
| gewone lokale login | code ontvangen | **code ontvangen** |
| account-rollen in token | `manage-account`, `manage-account-links` | geen |
| `credentials` / `linked-accounts` | 200 / 200 | 401 / 401 |
| unlink IdP | 204, koppeling daadwerkelijk weg | 401 |

Eis: niemand mag zelf een IdP koppelen of ontkoppelen, en niemand mag zichzelf daarmee een wachtwoord geven. Lokale accounts blijven wel bestaan en moeten blijven werken.

Hangt samen met [keycloak-account-link-in-ui.md](keycloak-account-link-in-ui.md): auto-link op `automatic` is alleen verantwoord als de identiteit uit een vertrouwde IdP komt, en dat is precies wat deze omzeiling onderuit haalt.

## Het zijn twee onafhankelijke knoppen, geen één

De verleiding is te denken dat één instelling dit dekt. Dat is niet zo: koppelen en wachtwoord-zetten lopen via verschillende paden in Keycloak, met verschillende autorisatie. Alles hieronder is geverifieerd tegen de broncode van Keycloak 25.0, de versie die in productie draait.

### Knop 1: koppelen en ontkoppelen zit achter de rol `manage-account`

In `LinkedAccountsResource` (Keycloak 25.0) vereisen zowel toevoegen als verwijderen:

```java
auth.require(AccountRoles.MANAGE_ACCOUNT);
```

Alleen het *tonen* van de lijst mag ook met `view-profile` (`auth.requireOneOf(MANAGE_ACCOUNT, VIEW_PROFILE)`).

De standaard rolset is `String[] DEFAULT = {VIEW_PROFILE, MANAGE_ACCOUNT};` (`AccountRoles`), en die zit via `default-roles-<realm>` op elke gebruiker. **Haal `account: manage-account` uit dat composiet en laat `view-profile` staan**, en zelf koppelen of ontkoppelen is onmogelijk. Dit blokkeert meteen ook het zelf wijzigen van profielvelden, want `updateAccount()` vereist dezelfde rol, wat mooi aansluit op de identity-field-lock.

Let op de valkuil: `MANAGE_ACCOUNT_LINKS` bestaat wel als aparte rol, maar de account console v3 gebruikt hem in deze code niet. Die rol wegnemen doet dus niets.

### Knop 2: een wachtwoord zetten zit achter de required action `UPDATE_PASSWORD`

Dit loopt **niet** via de account-REST-API en dus ook niet via `manage-account`. De knop "Wachtwoord instellen" start een application initiated action richting de login-endpoint (`kc_action=UPDATE_PASSWORD`). Of de knop verschijnt, hangt aan `CredentialTypeMetadata.Builder.build(session)`:

```java
if (!verifyRequiredAction(session, instance.createAction)) {
    instance.createAction = null;
}
if (!verifyRequiredAction(session, instance.updateAction)) {
    instance.updateAction = null;
}
```

`verifyRequiredAction()` geeft false zodra de required action niet als **enabled** provider in de realm staat. Voor het wachtwoord-credential zijn `createAction` en `updateAction` allebei `UPDATE_PASSWORD`.

Knop 1 alleen is dus niet genoeg: met `manage-account` weg kan een gebruiker nog steeds een wachtwoord zetten, en op een `sso-support`-realm (inlogformulier zichtbaar) daarmee lokaal inloggen. De koppeling hoeft daar niet eens voor weg.

### Waarom knop 2 ook knop 1 half afdekt

`LinkedAccountsResource.removeLinkedAccount` weigert de laatste koppeling te verwijderen bij:

```java
!(getFederatedIdentitiesStream().count() > 1 || user.getFederationLink() != null || isPasswordSet())
```

met de fout `FEDERATED_IDENTITY_REMOVING_LAST_PROVIDER`. Zonder wachtwoord, zonder tweede IdP en zonder user-federation link kán er dus niet ontkoppeld worden. Dat is een vangnet, geen vervanging: het dekt het *toevoegen* van een koppeling niet af, en het helpt niet bij gebruikers die al een wachtwoord hebben.

Conclusie: voor de gestelde eis zijn beide knoppen nodig.

### Knop 3: de hele account-functionaliteit uit bij het opstarten van de server

Er is een derde weg die beide bovenstaande knoppen in één klap overbodig maakt: de account console en de account-REST-API zijn Keycloak-features die je bij het opstarten kunt uitzetten. `account:v3` (Account Console) en `account-api:v1` (Account Management REST API) staan standaard aan en zijn allebei uit te zetten met `--features-disabled="account,account-api"`.

Dat de API daarachter echt afgeschermd wordt, is zichtbaar in de code: `AccountRestService.credentials()` begint met `checkAccountApiEnabled()`. Zonder die feature is er dus geen account-REST-API, en zonder console is er geen UI die hem aanroept. Geen koppelbeheer, geen wachtwoordknop, niets.

In onze deployment gaat dat via een env-var, en dat patroon staat er al: `infrastructure/bootstrap/infrastructure/keycloak/controller/base/deployment.yaml` zet nu `KC_FEATURES: "admin-api"`. Daar komt `KC_FEATURES_DISABLED: "account,account-api"` naast.

Voordelen ten opzichte van knop 1 en 2:

- **Geen OPI-wijziging nodig.** Het plumbing-gat hieronder (blueprints die realm-instellingen niet doorgeven) hoeft er niet eerst voor gedicht te worden. Dat scheelt de hele stap 1 van het voorstel.
- **Geen prijs voor lokale accounts.** Inloggen met een wachtwoord blijft gewoon werken; alleen zelfbediening verdwijnt, en die willen we juist weg.
- **Eén regel, direct te controleren.**

Nadelen en dingen om te verifiëren:

- **Serverbreed, niet per realm.** Het geldt voor elk realm op die Keycloak, inclusief `rig-platform`. Voor ZAD lijkt dat geen bezwaar: er is niets in de portal dat naar de Keycloak account console verwijst. Het menu heeft wel een link `/account`, maar dat is een ZAD-pad zonder route in `web/` of `api/`, dus een dode link en een los dingetje.
- **De AIA-route blijft waarschijnlijk open.** "Wachtwoord instellen" werkt via `kc_action=UPDATE_PASSWORD` op de login-endpoint, en die endpoint hoort niet bij de account-feature. Een handmatig samengestelde URL met een geldige client zou dus alsnog een wachtwoord kunnen zetten. Dat is precies waarom knop 2 er als aanvulling bij hoort. **Te verifiëren in de sandbox**, want dit is de scharnierende aanname.
- **Features zijn build-opties.** Ze bij het opstarten meegeven zorgt voor re-augmentatie en dus een tragere start, tenzij het image al voorgebouwd is. We doen dit al met `KC_FEATURES`, dus het patroon is niet nieuw, maar de opstarttijd is het checken waard.
- Het raakt ook andere account-functies, zoals het overzicht van eigen sessies en apparaten. Voor deze doelgroep vermoedelijk geen verlies, maar het is wel een keuze.

## De prijs, en dat is een productbeslissing

`UPDATE_PASSWORD` uitzetten geldt voor de hele realm, ook voor lokale accounts. Die blijven gewoon inloggen met hun bestaande wachtwoord, dus "lokale accounts blijven werken" klopt. Maar ze kunnen hun wachtwoord niet meer zelf wijzigen, en "wachtwoord vergeten" staat al uit (`resetPasswordAllowed` staat hardgecodeerd op `False`). Wachtwoordbeheer wordt dan een taak van de realmbeheerder.

Drie smaken:

- **A. Beide knoppen, hele realm.** Voldoet volledig aan de eis. Kost zelfbediening op wachtwoorden voor lokale accounts.
- **B. Beide knoppen op `sso-only`, alleen knop 1 op `sso-support`.** Op `sso-only` heeft een lokaal wachtwoord geen legitiem doel (browser-flow is External IDP Redirector, `authenticateByDefault: true`, registratie uit). Op `sso-support` is lokaal inloggen bedoeld gedrag, dus daar blijft wachtwoord-zelfbediening. Nadeel: op `sso-support` blijft de omzeiling van SSO mogelijk, alleen niet meer het ontkoppelen. En het UI-veld "Keycloak template" heeft `sso-support` als default, dus dat is de meerderheid van de projecten.
- **C. Eigen required-action provider** die `UPDATE_PASSWORD` weigert zodra de gebruiker een federated identity heeft, en toestaat voor pure lokale accounts. Dat geeft precies wat er gevraagd wordt zonder prijs, maar het is een eigen jar in het Keycloak-image. Er is precedent (`keycloak-migration/custom-mapper/`), maar het is wel onderhoud, en het moet mee in de 26-upgrade.

- **D. Knop 3 (account-feature uit) plus knop 2.** De feature-vlag haalt alle zelfbediening weg zonder OPI-wijziging, en de required action dekt de AIA-route af die daarnaast blijft bestaan. Lokale accounts loggen gewoon in.

Gekozen: **A**, met de vervanging voor zelfbediening in fase 2. De redenering staat onder "Besluit" bovenaan. Kort: de prijs in de alinea hierboven blijkt in onze praktijk vrijwel nul, want wij gebruiken de flow die knop 2 zou breken helemaal niet, en de eis is per gebruiker geformuleerd waar B per realm werkt.

## Het plumbing-gat: blueprints zijn vandaag geen bron van waarheid

De vraag was of dit in de blueprints kan. Dat kan, maar niet zonder eerst een gat te dichten, want **realm-instellingen uit de blueprints komen op dit moment grotendeels niet in Keycloak terecht**.

`KeycloakYamlHandler._process_realms()` leest uit een `realms:`-item alleen `name`, `displayName`, `ssoSessionIdleTimeout`, `ssoSessionMaxLifespan` en de vier `events`-velden, en geeft die door aan `KeycloakConnector.create_realm()`. Die bouwt een `realm_data`-dict waarin alle overige velden **hardgecodeerd** staan.

Het bewijs staat in de repo zelf. `configs/keycloak/sso-support.yaml` zet:

```yaml
registrationAllowed: true
loginWithEmailAllowed: true
resetPasswordAllowed: true
verifyEmail: true
```

Geen van die vier bereikt Keycloak; `create_realm()` zet ze hard op `False`. Toevallig is dat de veilige kant op, maar wie de blueprint leest denkt iets anders dan wat er draait, en een nieuwe regel in de blueprint doet vandaag simpelweg niets.

Daarbij komt hetzelfde reconcile-gat als bij de identity-field-lock: op de "realm bestaat al" tak past `create_realm()` alleen de sessie- en event-instellingen toe, met een expliciete reden (een volledige update zou `browserFlow` resetten en de External IDP Redirector slopen). Bestaande realms krijgen nieuwe instellingen dus sowieso niet.

Geen van beide knoppen is trouwens een veld op de realm-representatie:

- knop 1 gaat via het composiet `default-roles-<realm>`, dus rollen toevoegen/verwijderen op een composite role
- knop 2 gaat via `PUT /admin/realms/{realm}/authentication/required-actions/UPDATE_PASSWORD` met `enabled: false`

Beide zijn wel realm-scoped, dus ze passen prima als eigen secties in de blueprint zodra de handler ze kent.

## Wat er nog moet gebeuren

1. **`scripts/keycloak_self_service_report.py` draaien op productie**, ook over `rig-platform`. Gebruikers met een openstaande `UPDATE_PASSWORD` raken klem zodra de actie uit gaat, dus die moeten eerst opgelost. In de sandbox is dat nul, maar dat zegt niets over productie.
3. **Bestaande wachtwoorden opruimen.** De knoppen omzetten verandert niets aan wat er al is: wie al een wachtwoord heeft gezet, houdt de omzeiling. Dat geldt in elk geval voor het testaccount waarmee dit is ontdekt. Het rapport uit stap 2 levert de lijst en de `DELETE`-paden; opschonen daarna, in de stijl van de service-orphan sweep.
4. **Besluit over de platform-realm.** De `bootstrap*.yaml` zijn bewust niet aangeraakt. Daar leeft de `sso-rijk`-koppeling, dus dezelfde omzeiling bestaat daar een niveau hoger en weegt zwaarder. De reden om te wachten is dat onbekend is of er lokale break-glass-accounts op `rig-platform` staan die je niet wil klemzetten; stap 2 beantwoordt dat.
5. **Los daarvan, lagere prioriteit: `directAccessGrantsEnabled`.** Deployment-clients krijgen die op `True` (`connectors/keycloak.py`, zowel de deployment-client als de extra clients). Dat is de ROPC-flow: met gebruikersnaam, wachtwoord en het client secret is een token te halen zonder browser en zonder IdP. De clients zijn confidential, dus dit is geen open deur, maar ZAD-applicaties gebruiken de authorization-code flow, dus de vlag mag uit. Aparte, kleine opruiming.

## Fase 2: wachtwoord-zelfbediening terugbrengen, buiten Keycloak om

Ontwerpnotitie, niet gebouwd. Wacht op de mailserver.

Fase 1 haalt geen functie weg die vandaag werkt, maar het bevriest de situatie wel: een lokale gebruiker die zijn wachtwoord kwijt is of wil wijzigen, moet bij de realmbeheerder zijn. Dat is nu al zo, maar het is geen eindstation.

Binnen Keycloak is dat niet meer op te lossen, en dat is geen toeval: zowel `execute-actions-email` als "wachtwoord vergeten" (`resetPasswordAllowed`) zetten intern dezelfde required action `UPDATE_PASSWORD`. Wie die uitzet, zet die twee ook uit. Een mailserver koppelen aan Keycloak lost dit dus níet op.

Buiten Keycloak wél, en zonder eigen Java. Wij bezitten het aanmaakpad al: bij de invite-registratie kiest de gebruiker zijn wachtwoord in óns formulier (`invite-register.html.j2` → `invite_manager.complete_local_invite`) en wij schrijven het via de admin-API weg. Wachtwoord wijzigen en resetten is dezelfde code met een ander startpunt. Het bezwaar dat realm-lokale gebruikers geen ZAD-account hebben en die pagina dus eigen authenticatie nodig heeft, verdwijnt met een mailserver: de gemailde token ís de authenticatie, precies zoals de invite-link dat vandaag al is.

Schets:

- Pagina "wachtwoord instellen": e-mailadres invullen, wij mailen een token-link. Geen uitspraak doen over of het adres bestaat, anders is het een user-enumeratie-orakel.
- Bij het inwisselen van de token controleren of de gebruiker een federated identity heeft (`GET /admin/realms/{realm}/users/{id}/federated-identity`). Zo ja: weigeren en doorverwijzen naar SSO Rijk. Dat is precies het onderscheid per gebruiker dat Keycloak zelf niet kan maken, en in Python is het één aanroep.
- Nieuw wachtwoord schrijven via dezelfde weg als de invite-flow, met dezelfde eisen uit `invite_manager._validate_password`.
- Token eenmalig en kortlevend, en de bestaande sessies van die gebruiker intrekken na een wijziging.

Openstaand: welke mailserver het wordt en of die vanuit ODCN bereikbaar is. Let op dat ODCN geen route heeft naar 145.21.0.0/16, wat SMTP daar eerder al blokkeerde.

## Wat dit niet oplost

Een realmbeheerder kan zelf een wachtwoord op een gebruiker zetten via de admin console. Dat is geen gat maar een bevoegdheid: wie realmbeheer heeft, bepaalt per definitie identiteiten in dat realm. Wel iets om mee te wegen bij de vraag wie realmbeheerder is.
