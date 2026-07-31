# Zelfbediening op identiteit dichtzetten in projectrealms (IdP koppelen/ontkoppelen en wachtwoord zetten)

Status: ontwerpnotitie (2026-07-31). Niet gebouwd. Aanleiding: een gebruiker kan in de account console van een projectrealm zelf een wachtwoord zetten en vervolgens de koppeling met de `rig-platform-oidc` IdP verwijderen. Vanaf dat moment logt die persoon lokaal in, zonder SSO Rijk en zonder de platform-realm. Praktisch getest en werkend bevonden.

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

Voorkeur: **D als eerste stap**, want die vraagt één regel in de Keycloak-deployment en niets aan het plumbing-gat hieronder. Knop 1 en 2 per realm blijven daarna zinvol als verdediging in de diepte en voor het geval de account console ooit weer aan moet. De open productvraag is dan alleen nog of `sso-support` lokale accounts als bedoelde functie houdt of niet.

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

## Voorstel

0. **Eerst knop 3**, want die staat los van alles hieronder: `KC_FEATURES_DISABLED: "account,account-api"` in de Keycloak-deployment, in de sandbox verifiëren dat de account console weg is én of de AIA-route `kc_action=UPDATE_PASSWORD` dan nog werkt. Die uitkomst bepaalt hoe dringend de rest is.
1. **Blueprint-secties toevoegen** voor required actions en voor de samenstelling van `default-roles-<realm>`, verwerkt door `KeycloakYamlHandler`. Idempotent, en aangeroepen op zowel de create- als de reconcile-weg, precies zoals `lock_identity_fields()`.
2. **Knoppen zetten in `sso-only.yaml`**, en een besluit nemen over `sso-support.yaml` (zie smaken hierboven).
3. **Bestaande wachtwoorden en koppelingen opruimen.** De knoppen omzetten verandert niets aan wat er al is: wie al een wachtwoord heeft gezet, houdt de omzeiling. Dat geldt in elk geval voor het testaccount waarmee dit is ontdekt. Nodig is een controle per realm (welke gebruikers met een federated identity hebben een wachtwoord-credential) plus opruimen via `DELETE /admin/realms/{realm}/users/{id}/credentials/{credentialId}`. Rapport eerst, opschonen daarna, in de stijl van de service-orphan sweep.
4. **Verifiëren vóór uitrol** dat een uitgezette `UPDATE_PASSWORD` geen gebruikers klemzet die die required action al openstaan hebben, bijvoorbeeld doordat een beheerder een wachtwoord met "Temporary" heeft gezet. Dat is precies het soort detail dat pas in de sandbox blijkt.
5. **Los daarvan, lagere prioriteit: `directAccessGrantsEnabled`.** Deployment-clients krijgen die op `True` (`connectors/keycloak.py`, zowel de deployment-client als de extra clients). Dat is de ROPC-flow: met gebruikersnaam, wachtwoord en het client secret is een token te halen zonder browser en zonder IdP. De clients zijn confidential, dus dit is geen open deur, maar ZAD-applicaties gebruiken de authorization-code flow, dus de vlag mag uit. Aparte, kleine opruiming.

## Wat dit niet oplost

Een realmbeheerder kan zelf een wachtwoord op een gebruiker zetten via de admin console. Dat is geen gat maar een bevoegdheid: wie realmbeheer heeft, bepaalt per definitie identiteiten in dat realm. Wel iets om mee te wegen bij de vraag wie realmbeheerder is.
