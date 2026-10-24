# Leden en API-tokens

Een project heeft leden, en een lid kan API-tokens hebben. Een lid is een mens met een
e-mailadres, of een service account met een naam. Allebei staan ze in `users:` van het
projectbestand, allebei dragen ze dezelfde vier rollen, en wat hun token mag volgt uit die
rol.

De afwegingen staan in `features/futures/rechten-modellen-en-tokens.md` (paragraaf 5) en
`features/futures/rechten-plan-van-aanpak.md` (fase 1 en fase 5); dit document beschrijft
wat er gebouwd is.

## Wat er in het projectbestand staat

```yaml
schema-version: '2.12'
users:
  - email: beheerder@rijksoverheid.nl
    role: admin
    tokens:
      - name: cli
        expires: '2027-01-14T09:12:00+00:00'
        hash: 9f2c...64 hextekens
  - type: service-account
    name: bouwstraat
    role: developer
    tokens:
      - name: pijplijn
        expires: '2027-06-01T00:00:00+00:00'
        hash: 1b7e...64 hextekens
```

| veld | wat het zegt |
|---|---|
| `type` | `person` (de standaard, mag weg) of `service-account` |
| `email` | alleen bij een mens; bij een service account verboden |
| `name` | alleen bij een service account, en dan een DNS-label |
| `role` | `admin`, `owner`, `member` of `developer`, voor allebei dezelfde |
| `tokens[].name` | korte naam, uniek binnen dit lid; hiermee trek je hem in |
| `tokens[].expires` | verplicht; een token zonder vervaldatum is niet aanmaakbaar |
| `tokens[].hash` | sha256 van de waarde, in hex. Nooit de waarde zelf |

Een token is 32 willekeurige bytes (256 bits) met het voorvoegsel `zad_`. Alleen de hash
gaat het bestand in, dus na uitgifte leest niemand hem meer terug, ook de uitgever niet.
Dat is wat `config.api-key` miste: die stond AGE-versleuteld in het bestand en was dus per
definitie terug te halen.

**Intrekken is een commit.** Het tokenblok verdwijnt uit het projectbestand, en daarmee
staat in de historie wie wanneer welk token introk. Dat is de prijs van tokens die bij hun
lid staan in plaats van in een tabel, en tegelijk de winst.

## Wat een rol opent

De handelingencatalogus koppelt een ROUTE aan een HANDELING; de rol koppelt een HANDELING
aan een LID. Samen bepalen die twee wat een token mag.

| rol | wat zijn token opent |
|---|---|
| `admin`, `owner` | alle handelingen op dit project |
| `member`, `developer` | alleen `LEESHANDELINGEN` |
| iets anders | niets |

Dat is exact de grens die `require_project_edit_access` in de interface trekt: een token
opent nooit meer dan zijn eigenaar op het scherm kan. Scopes per token bestaan niet, zodat
er geen tweede plek is waar rechten staan en die uit de pas kan lopen.

De rol wordt bij ELKE beslissing opnieuw uit het projectbestand gelezen. Verlaat iemand het
project of krijgt hij een lagere rol, dan werkt dat dezelfde seconde door op al zijn
tokens, zonder dat er iets ingetrokken hoeft te worden.

### Wat een service account nooit mag

`MENS_VERPLICHT` (`opi/api/principaal.py`) zijn de handelingen die een geverifieerde MENS
vragen, ongeacht het doelwit. Vandaag staat daar `token.beheren` in: wie een token kan
maken kan zijn eigen bewijsmiddel vermenigvuldigen, en dat hoort geen pijplijn te kunnen.
Een service account krijgt daarop altijd een 403 met `cause: tokensoort-niet-toegestaan`:
een eigen reden en niet "te weinig rechten", want zijn rol klopt en zijn SOORT niet.

### De step-up op een token

Een handeling die productie raakt vraagt een VERSE tweede factor (RC-332), en met een
`zad_`-token loopt dat langs dezelfde weg als met een SSO-bearer. Het handhavingspunt zet
de eigenaar van een persoonlijk token op `request.state.user`, en dat is precies waar
`verify_api_step_up` (`opi/api/otp_step_up.py`) de persoon leest tegen wie de code uit
`X-ZAD-OTP` getoetst wordt. Een code van iemand anders aan een aanroep hangen kan dus niet.

| bewijsmiddel | productiehandeling zonder code | met een geldige code | met een foute code |
|---|---|---|---|
| token van een mens | 403 `otp-ontbreekt` | gaat door | 403 `code-ongeldig` |
| token van een service account | 403 `tokensoort-niet-toegestaan` | idem, de factor wordt niet eens geraadpleegd | idem |
| de oude projectsleutel | 403 `tokensoort-niet-toegestaan` | idem | idem |

Het antwoord is een problem-detail (RFC 7807) met `type: urn:zad:step-up-required`, zodat
een client ziet welke handeling het was, waarom ze een step-up vraagt en wat hij moet doen.
De eigen tweede factor richt een mens in op `/account/otp`.

**`MENS_VERPLICHT` en de step-upcatalogus zijn met opzet NIET dezelfde verzameling.** Ze
beslissen een andere vraag: `MENS_VERPLICHT` zegt of deze SOORT aanroeper dit ooit mag, en
dat is onvoorwaardelijk; een step-up weegt het DOELWIT, dus een deployment op productie
vraagt er een en dezelfde op acceptatie niet. `project.verwijderen` in `MENS_VERPLICHT`
zetten zou een pijplijn ook uitsluiten van een project zonder productie, en dat is een
verruiming van de weigering die niemand gevraagd heeft. Welke step-upactie bij welke
handeling hoort staat per route in `tests/test_handelingen.py`, zodat de twee catalogi niet
stil uit elkaar lopen.

## De handelingencatalogus

Een scope noemt HANDELINGEN en geen paden. Een lijst paden veroudert bij de eerstvolgende
route; een handeling niet, want een nieuwe route moet er een kiezen.

De catalogus is `Handeling` in `operations-manager/python/opi/core/handelingen.py`. Elke
token-bereikbare route wijst er precies een aan:

```python
@v2_router.put("/projects/{project_name}/deployments/{deployment_name}/image")
@handeling(Handeling.DEPLOYMENT_IMAGE_VERVANGEN)
@validate_api_token
async def update_image_v2(...): ...
```

`controleer_handelingen` draait bij het opstarten over `app.routes` en WEIGERT te starten
als een token-bereikbare route geen handeling noemt. Zonder die controle zou zo'n route
stilzwijgend alles openen wat het token mag.

De markering staat BOVEN de authenticatiedecorator, zodat hij op het object komt dat
FastAPI als endpoint registreert.

## Het handhavingspunt

`opi/api/principaal.py` vertaalt elk binnenkomend bewijsmiddel naar een principaal en
beslist een keer:

1. hash de gepresenteerde `X-API-Key` en zoek hem bij de tokens van de leden van het
   project uit de route;
2. een verlopen token telt niet mee;
3. een token van een ANDER project geeft dezelfde 401 als een onbekend token. Niet een 403:
   of een project bestaat is zelf al iets dat een onbevoegde aanroeper niet hoort te kunnen
   aflezen, en een verschil tussen die twee antwoorden maakt projectnamen aftastbaar;
4. bij een mens wordt ook de platformtoegang gelezen, want offboarding in het
   gebruikersbeheer raakt geen projectbestand;
5. daarna beslist `beslis()` op de rol van het lid, opnieuw uit het bestand gelezen.

De actor wordt SERVER-SIDE gezet, op de ingang, nooit uit een body:

| bewijsmiddel | actor in het logboek en in de git-trailer |
|---|---|
| token van een mens | `<e-mailadres>` |
| token van een service account | `service-account:<project>/<naam>` |
| oude projectsleutel | `project-api-key:<project>` |

## Een mens is geen service account

Elke lezer van `users:` die iets met een MENS doet (een Keycloak-gebruiker, een
uitnodiging, een mail, OTP, de inloglijst, de databaseconsole) gebruikt
`human_members(project)` uit `opi/services/project_leden.py`. Een service account heeft
geen postvak en geen tweede factor, dus zou daar een toegang opleveren die niemand kan
gebruiken.

`tests/test_human_members_guard.py` somt elke lezer op en faalt als er een bijkomt. Een
nieuwe lezer moet dus kiezen: alle leden, of alleen de mensen.

## Het Team-blok bewerken raakt de tokens niet

`users:` draagt sinds 2.12 twee soorten lid en de tokenhashes van beide, terwijl het
bewerkmodaal van het tabblad Team alleen het e-mailadres en de rol van een MENS rendert.
Dat formulier bezit het pad `users` als geheel (`USERS_SEQUENCE_EDITABLE`), dus zonder
voorziening gooide een collega toevoegen elk token en elk service account van het project
weg, onder een commit die "Projectleden bijgewerkt" heet.

`opi/forms/wizard/leden.py` houdt daarom uit het formulier wat het niet bewerkt, en zet het
bij het opslaan terug, net zoals `opi/forms/wizard/secrets.py` dat voor versleutelde
waarden doet:

- `leden_voor_formulier` vult de modaal met alleen de mensen, zonder hun tokens. Een
  service-accountregel in het formulier zou als lid zonder adres landen, en geen uitkomst
  laat hem overleven: leegmaken valt op de verplichte-veldcontrole, weghalen verwijdert het
  account en een adres invullen laat het schema de regel weigeren;
- `zet_verborgen_leden_terug` zet bij het opslaan de service accounts terug zoals ze in het
  bestand staan, en de tokens van een mens op zijn e-mailadres. Een lid dat uit de lijst is
  gehaald is zijn tokens wel kwijt, want dat is wat het verwijderen van een lid hoort te
  doen; er gaat een regel in het log over welke dat waren.

Dezelfde twee stappen staan op de volledige bewerkwizard
(`GET /forms/wizard/edit-project/edit/{project}`), die hetzelfde Team-blok draagt. Daar is
het erger dan verliezen: die weg merget een stap per INDEX, dus een lid weghalen schoof zijn
tokenhash naar de rij die zijn plaats innam. Dat lid was dan niet ingetrokken maar OMGEKAT,
en legitimeerde zich daarna als degene op wiens rij zijn hash beland was, met diens rol.
`tests/forms/test_leden_overleven_het_teamformulier.py` legt dat per weg vast, en leest de
bron van elke formulierweg zodat een derde weg niet zonder deze stappen kan beginnen.

Een service account toevoegen, verwijderen of een token uitgeven gaat dus niet via dit
formulier maar via de routes hieronder.

## De routes

Alles hangt onder `/api/v2/projects/{project}/leden`. Legitimatie gaat met een
SSO-bearertoken en NIET met een API-token: een token aanmaken vraagt een geauthenticeerde
persoon, want de aanroeper hoort niet zelf te kiezen wie hij is.

| route | wat het doet |
|---|---|
| `GET /leden` | de leden met hun rol en hun tokens, zonder hashes |
| `POST /leden` | een service account toevoegen (beheerder) |
| `DELETE /leden/{naam}` | een service account verwijderen, met zijn tokens |
| `PUT /leden/{naam}/rol` | de rol van een lid wijzigen (beheerder) |
| `POST /leden/{naam}/tokens` | een token uitgeven |
| `POST /leden/{naam}/tokens/{token}/:roteer` | intrekken en opnieuw uitgeven |
| `DELETE /leden/{naam}/tokens/{token}` | intrekken |

Wie mag uitgeven: een beheerder voor de service accounts van zijn project, een mens voor
zichzelf. Een token voor een ANDER mens maakt niemand, ook een beheerder niet: dat zou een
bewijsmiddel zijn dat iemand draagt zonder te weten dat het bestaat. Intrekken mag een
beheerder wel voor iedereen, want offboarding moet kunnen zonder op die persoon te wachten.

**Een platformbeheerder die geen lid is van het project mag hier niets**, ook niet zijn
eigen token intrekken: wat iemand hier mag staat in `users:` van het projectbestand, en wie
daar niet staat heeft er geen bewijsmiddel. Zijn weg is zich als lid toevoegen, en dat is
een commit die iemand kan reviewen. Het tabblad Team toont hem daarom ook geen knoppen
(`is_projectbeheerder`), zodat de knop en de weigering erachter dezelfde bron lezen.

```bash
# een token uitgeven; de waarde staat EEN keer in dit antwoord
curl -X POST https://zad.sandbox.rijksapp.dev/api/v2/projects/demo/leden/bouwstraat/tokens \
  -H "Authorization: Bearer $SSO_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"naam": "pijplijn", "dagen": 365}'

# en hem daarna gebruiken, net als de oude projectsleutel
curl https://zad.sandbox.rijksapp.dev/api/v2/projects/demo \
  -H "X-API-Key: zad_..."
```

In het portaal staat hetzelfde op het tabblad **Team** van een project. De uitgegeven
waarde staat daar precies een keer op het scherm; de sessie draagt alleen een verwijzing,
want de cookie-sessie van Starlette is ondertekend en niet versleuteld. Het antwoord dat
die waarde draagt krijgt `Cache-Control: no-store`, anders mag een browser de pagina op
schijf zetten en toont de terug-knop het token opnieuw, ook nadat het server-side weg is.

Logt er ergens toch een token mee, dan maskeert `SecretRedactionFilter`
(`opi/utils/logging_redact.py`) de vorm `zad_` plus 40 of meer url-veilige tekens. Vandaag
logt niets de waarde; dit is het vangnet voor de volgende logregel die hem meeneemt.

## De migratie van de projectsleutel

Schemaversie 2.12 haalt `config.api-key` uit het projectbestand en zet de sleutel voort als
een token:

```yaml
- type: service-account
  name: project-api
  role: admin
  tokens:
    - name: projectsleutel
      expires: '2031-01-01T00:00:00+00:00'
      hash: <sha256 van de oude sleutel>
```

Dezelfde waarde blijft dus werken en een draaiende pijplijn merkt niets, maar hij heeft nu
een eigenaar, een naam, een vervaldatum en een rol, en hij is in te trekken zonder het
project te raken.

**Waarom rol `admin`**: de projectsleutel opende de hele schrijfkant van een project, tot
en met het verwijderen ervan en het terugzetten van een backup. Een smallere rol zou de
migratie draaiende pijplijnen laten breken, en de keuze om een pijplijn minder te geven
hoort bij de projectbeheerder (verlaag de rol, of verwijder het account), niet bij de
migratie.

Die rol telt NIET mee voor de eis dat een project een beheerder heeft
(`validate_admin_user_exists`). Anders haalt een bestand die eis ook als er geen MENS meer
`admin` is, en dan kan niemand nog een token intrekken of een rol wijzigen terwijl het
token van `project-api` doorloopt tot 2031.

De hash moet over de PLATTE sleutel, en die staat AGE-versleuteld in het bestand. Gaat het
ontsleutelen niet (geen platform-KEK), dan BLIJFT `config.api-key` staan: weghalen zou de
sleutel weggooien die we bij een volgende ronde, met de KEK, wel kunnen omzetten. Zo'n
bestand blijft GELDIG, want het schema van 2.12 laat het veld nog toe. Legitimeren doet die
sleutel dan NIET: `_resolve_plaintext_api_key` registreert niets als hij niet open gaat.
Vroeger registreerde hij de ciphertext, en dan was die zijn eigen wachtwoord: wie het
projectbestand kon lezen legitimeerde zich ermee als projectsleutel, terwijl de pijplijn
met de echte waarde juist buiten stond. Het veld laten staan is met opzet: een schema dat
het veld afkeurt laat elke verwerking van dat bestand stil stranden (de `dp-bn7`-klasse die
`tests/test_upgrade_safety_replay.py` moet vinden, en die per ontwerp zonder sleutel draait).
Het veld verdwijnt pas uit het schema als de meting hieronder zegt dat niemand de oude
sleutel meer gebruikt.

De migratie is idempotent: hij draait bij elke lezing van een bestand dat nog niet is
weggeschreven, en levert dan steeds hetzelfde op. Daarom is de vervaldatum een vaste datum
en geen `nu + N dagen`.

Een nieuw project krijgt geen `config.api-key` meer maar meteen zo'n `project-api`-account.
Een token krijgt dat account alleen als de aanroeper de waarde ook te zien krijgt, en dat is
bij `POST /api/v2/projects`, die hem een keer teruggeeft. Dat eerste token verloopt na
`TOKEN_MAX_DAGEN_SERVICE_ACCOUNT` dagen (standaard 365), het volle plafond voor een
service-accounttoken en geen uitzondering erop: de vaste datum hierboven hoort bij de
MIGRATIE en haar idempotentie, en een uitgifte heeft die reden niet. Een project uit de
WIZARD begint dus met een account zonder tokens: een token dat niemand kent is een
bewijsmiddel dat niemand kan gebruiken en niemand opruimt. De beheerder geeft er een uit
op het tabblad Team.

Gereedschap dat zonder browser een bewijsmiddel nodig heeft geeft er daarom zelf een uit:
`sandbox_api.geef_projecttoken` doet dat over het tabblad Team met een ondertekende
sessiecookie, en de e2e-suite en `scripts/sandbox_project_tool.py` lopen beide die weg.
Teruglezen van een bestaand token kan niet, ook niet door wie het uitgaf.

`GET /api/v2/projects/{project}/api-key` antwoordt `410 Gone` zodra een project gemigreerd
is, met een verwijzing naar de tokenroute.

## De meting

Elke geslaagde legitimatie wordt geteld per project en per soort bewijsmiddel, als
Prometheus-teller `opi_api_credential_calls{project,credential}`. De labelwaarde
`projectsleutel` is de oude weg; `persoon` en `service-account` zijn de nieuwe. Zonder die
telling is "de projectsleutel verdwijnt zodra niemand hem meer gebruikt" een wens.

## Wat hier NIET in zit

- de handelingencatalogus uitbreiden naar alle 158 routes, en de handhaving naar een
  AuthZEN-vorm brengen (fase 1 tot 4 van `rechten-plan-van-aanpak.md`);
- agents als eigen soort aanroeper (`via_agent`, `on_behalf_of`, fase 6);
- `last_used_at` per token: dat komt uit een audittabel, niet uit het projectbestand;
- `ADMIN_API_KEY` en `MASTER_API_KEY` omzetten naar tokens (4.3);
- de rollenopschoning `owner` / `member` / `operator` (4.1).
