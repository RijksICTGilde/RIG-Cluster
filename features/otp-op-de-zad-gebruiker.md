# OTP op de ZAD-gebruiker

## Wat het is

Elke ZAD-gebruiker kan in het portaal zelf een 2-factor registreren: een TOTP-geheim
dat hij in een authenticator-app zet, zoals Google Authenticator. ZAD verifieert die codes
zelf. Er is geen tweede soort factor en er zijn geen herstelcodes: raak je je app kwijt,
dan reset een platformbeheerder je factor en registreer je opnieuw.

Sinds RC-332 vraagt een handeling die productie raakt erom: zie
[Welke handelingen een step-up vragen](#welke-handelingen-een-step-up-vragen).

## Welke handelingen een step-up vragen

De rol bepaalt nog steeds OF iemand iets mag. De step-up bepaalt dat hij het NU bewust
doet en dat hij het echt is (BIO2 8.05 en 8.32).

| handeling | vraagt een step-up als |
|---|---|
| een project verwijderen | een van zijn deployments effectief op `productie` staat |
| een deployment verwijderen | de effectieve markering van die deployment `productie` is |
| een component verwijderen | een deployment op `productie` dat component gebruikt |
| de OTAP-markering wijzigen | de wijziging `productie` VERLAAT; `productie` zetten vraagt niets |
| een wijzigende beheerroute | altijd (RC-377): platformbeheer raakt het hele cluster |
| gebruikersbeheer in het portaal (`/admin/users/*`) | altijd (RC-409): dezelfde regel als de API-tegenhanger |
| een token uitgeven, roteren of intrekken | altijd (RC-387) |
| een rol wijzigen, een service account toevoegen of verwijderen | altijd (RC-387) |
| de omgevingsvariabelen van een component bekijken of wijzigen | altijd (RC-388): ze kunnen geheimen bevatten |

De zes handelingen van RC-387 wegen de omgeving niet: met een token kan iemand blijvend bij
een project, ook als dat project niet op productie staat. Zie
[leden-en-api-tokens.md](leden-en-api-tokens.md). Env-vars (RC-388) wegen de omgeving om de
andere reden niet: ook in ontwikkeling kan er een geheim in staan. Zie
[omgevingsvariabelen-achter-step-up.md](omgevingsvariabelen-achter-step-up.md).

Een project heeft zelf geen markering (zie [otap-markering.md](otap-markering.md)), dus
"een project in productie" is afgeleid: `highest_environment` van zijn deployments.

Die regels staan op een plek, in `requires_production_step_up`
(`opi/core/step_up.py`); een route verklaart met `@requires_step_up(...)` dat ze eronder
valt, met een conditie die het doelwit weegt of met `vraagt_altijd_step_up`
(`opi/services/step_up_conditions.py`) als de handeling er altijd een vraagt. Welke routes
dat zijn houdt `tests/test_step_up_routes.py` vast.

Nog NIET: een backup in productie verwijderen. Dat is de volgende kandidaat, met hetzelfde
mechanisme.

### Het venster

Een geslaagde step-up dekt standaard vijf minuten (`STEP_UP_WINDOW_MINUTES`). Het venster
**schuift niet mee**: een handeling leest de stempel en zet hem nooit opnieuw, dus na het
venster is het voorbij, hoeveel handelingen er intussen ook langskwamen. Eén instelling
voor alles: een langer venster dekt ook de verwijderacties en de tokenhandelingen langer.

### Verhoogde rechten: platformbeheer zit erachter

Een platformbeheerder is een gewone gebruiker die zijn eigen projecten ziet, tot hij een
verse step-up heeft. Pas dan krijgt hij de beheerpagina's, alle projecten en `[admin, ops]`
op elk project. `ADMIN_EMAILS` zegt dus wie MAG verhogen, de step-up zet het aan.

In het portaal kiest hij **Verhoogde rechten** in het accountmenu. Dat opent een popup
(`nldd-modal-dialog`) waarin hij activeert met een code of het eerder beëindigt. Een
beheerpagina zonder step-up stuurt door naar het dashboard met de popup open, en na de code
terug naar die pagina. Elke route achter `require_platform_admin` is
daarmee ook gedekt door een verse code, dus ook het beoordelen van aanvragen.

Op een project waar hij geen lid van is, geeft een verlopen venster een gewone 403: activeer
opnieuw via het menu. `is_elevated_admin` (`opi/core/step_up.py`) is de ene plek die dit
beslist; zonder ingang die de toestand zet is het antwoord nee, dus een worker of een
vergeten route heeft nooit beheerrechten.

### In het portaal

De handeling wisselt zichzelf in voor een dialoog met een OTP-veld. Een geldige code zet
de step-up in de sessie, waarna de dialoog de oorspronkelijke handeling alsnog uitvoert.
Er zijn geen herstelcodes, dus alleen een code uit de app telt. Wie nog geen factor heeft
ingericht ziet in diezelfde dialoog de weg naar `/account/otp`; registreren vraagt zelf
nooit een step-up.

Bij een bewerking in de modal-wizard herhaalt de dialoog niet het pad van het verzoek maar
de `/confirm`-route van die flow, met het wizard-token: de staproute eist een JSON-body die
de bodyloze herhaling niet heeft, en `/confirm` leest alles uit de sessiestaat bij dat
token.

Formulieren op het tabblad Team die een step-up vragen tonen de dialoog in plaats van het
formulier. De formuliervelden gaan mee in verborgen invoervelden; met een geldige code
voert het formulier de handeling uit.

De meeste handelingen zijn een POST, en dat is wat de herhaling uitvoerde. Sinds RC-388
kent de dialoog ook een GET als methode: het omgevingsvariabelen-venster LEEST iets, en
herhalen met een POST zou er iets laten wijzigen waar alleen een lezing hoorde. De methode
reist mee in het verborgen veld `vervolg_methode`; alles behalve GET valt terug op POST.

### Te zien in het accountmenu

Zolang je een verse step-up hebt, toont het accountmenu rechtsboven een groen schild
(`shield-check-mark`) in plaats van het gewone persoonsicoon, en bovenaan het uitklapmenu staat
"Verhoogde rechten tot 14:32" (uitgeschakeld, alleen ter informatie). Het item heeft ook een
toegankelijk label, want kleur alleen is niet genoeg. Het eindtijdstip komt uit
`step_up_until()` in `opi/core/step_up.py`, `render()` in `opi/web/lotc_switch.py` zet het als
`verhoogd_tot` in elke pagina, en de kleur staat in `static/css/lotc-app.css` (`.menu-verhoogd`).

Er is bewust geen timer: de pagina bepaalt dit bij het laden. Verloopt het venster terwijl je op
een pagina blijft, dan blijft het schild staan tot je naar de volgende pagina gaat. De eindtijd in
het menu is een tijdstip en geen "nu", dus die blijft kloppen.

### Via de API en de CLI

Op de API is de step-up dezelfde toestand, met één call in plaats van een dialoog. Daarna werk
je door zonder code, tot het venster voorbij is.

| je legitimeert je met | activeren | geldt voor |
|---|---|---|
| SSO-token (`Authorization: Bearer`) | `POST /api/v2/step-up` | alles wat je mag, in heel ZAD |
| persoonlijk `zad_`-token (`X-API-Key`) | `POST /api/v2/projects/{project}/step-up` | dit ene token, dus dit ene project |

```bash
curl -X POST "https://zad.rijksapps.nl/api/v2/step-up" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"code": "123456"}'

curl -X DELETE "https://zad.rijksapps.nl/api/v2/projects/mijnproject/productie" \
  -H "Authorization: Bearer $TOKEN"
```

Het antwoord noemt `valid_until`. `DELETE` op dezelfde route beëindigt de step-up eerder. De
toestand hoort bij één bewijsmiddel: een SSO-token bij zijn inlogsessie (de
`sid`-claim, die een token-refresh overleeft) en een persoonlijk token bij zijn eigen hash. Een
code in de ene sessie opent dus geen ander token van dezelfde persoon.

Wat dat beschermt verschilt per bewijsmiddel. Een gelekt **persoonlijk token** kan niet
meeliften op de step-up van zijn eigenaar: die hangt aan de hash van het token dat de code
gaf, dus een ander token van dezelfde persoon staat erbuiten. Een gelekt **SSO-token** wel: dat
hangt aan de `sid` van de inlogsessie, dus elk token uit diezelfde sessie draagt de step-up tot
het venster om is. Daarom is dat venster kort en schuift het niet mee. De toestand staat in de
tabel `api_step_ups`.

De code wordt getoetst tegen de **eigenaar van het token**. Er is geen manier om de code van
iemand anders aan een aanroep te hangen. Een **service account** en de oude **projectsleutel**
horen bij geen persoon en kunnen geen step-up krijgen: dat is altijd een 403.

Een platformbeheerder heeft op de API dezelfde grens als in het portaal: zonder step-up ziet zijn
SSO-token alleen zijn eigen projecten en geven ook de leesroutes van platformbeheer een 403. Die
rechten zijn er alleen op een SSO-token, niet op een persoonlijk token. Het logpaneel en de
log-WebSocket staan onder `/api/`, maar draaien op de portaalsessie en lezen dus de stempel
daarvan.

#### Het antwoord op een weigering

Een geweigerde aanroep krijgt 403 met een problem-detail (`application/problem+json`)
waarvan `type` op `urn:zad:step-up-required` staat. Daaraan hoort een client te zien dat
hij een step-up moet activeren en de aanroep moet herhalen. Een ontbrekend of ongeldig token
blijft 401.

| veld | wat het zegt |
|---|---|
| `action` | welke handeling een step-up vraagt, bijvoorbeeld `deployment-delete` |
| `reason` | waarom, bijvoorbeeld dat de deployment op productie staat |
| `remedy` | wat er moet gebeuren |
| `cause` | waarom er geweigerd werd, als waarde om op te sturen |
| `enroll_url` | waar je je factor inricht, alleen bij `factor-missing` |
| `blocked_until` | wanneer het weer mag, alleen bij `geblokkeerd` |

`cause` onderscheidt vijf gevallen: `otp-missing`, `factor-missing` (met
`enroll_url: /account/otp`), `code-invalid`, `geblokkeerd` (met `blocked_until`) en
`principal-kind-not-allowed` (de projectsleutel). `code-invalid` dekt zowel een verkeerde
als een al gebruikte code: `detail` zegt nooit welk deel van een code fout was.

Elke route met `@requires_step_up` documenteert dit in de OpenAPI: de 403 met het schema
`StepUpProblem`. Dat wordt uit de markering op de route afgeleid (`custom_openapi` in
`opi/server.py`), en `tests/test_step_up_routes.py` faalt als een step-up-route die
documentatie mist.

### Loggen

INFO bij elke geslaagde step-up en bij elke handeling die er een gebruikte; WARNING bij
een geweigerde handeling. De code staat nooit in de log. Bij een API-aanroep noemt de
regel de aanroeper (de eigenaar van het token, of "een sleutel zonder gebruiker") en de
reden van de weigering, in dezelfde woorden als `cause`.

## Niet te verwarren met de OTP op de Keycloak-realm-admin

Er zijn er twee, met verschillende doelen. Zie ook
[keycloak-realm-admin-otp.md](keycloak-realm-admin-otp.md).

| | OTP op de ZAD-gebruiker (dit) | OTP op de realm-admin |
|---|---|---|
| hangt aan | een persoon | een gedeeld service-account per projectrealm |
| beschermt | een handeling in ZAD | de Keycloak-console |
| geheim staat in | de kolom `otp_secret` op `users`, AGE-versleuteld | het projectbestand, AGE-versleuteld |
| wie verifieert | OPI zelf (`opi/utils/totp.py`) | Keycloak |
| wie kent het geheim | alleen de gebruiker (en de versleutelde kolom) | elke projectbeheerder, want gedeeld |

## Registreren

1. Ga naar **Account** (je naam rechtsboven) en kies **2-factor inrichten**, of ga
   rechtstreeks naar `/account/otp`. Op `/account/otp` zelf heet de startknop **2-factor
   inrichten**.
2. Je logt eerst opnieuw in bij Keycloak, zie [Eerst opnieuw inloggen](#eerst-opnieuw-inloggen). Daarna kom je vanzelf terug en kun je beginnen.
3. Scan de QR, of voer de Base32-sleutel met de hand in.
4. Vul een code uit je app in. **Pas een geldige code maakt de factor echt**: tot dat moment is er
   niets opgeslagen.

Het zaad van een nog niet bevestigde registratie staat niet in de database. Daardoor
vervangt een nieuwe registratie het oude zaad pas als de nieuwe bevestigd is: breek je af,
dan blijft je oude factor gewoon werken.

### Bevestigen zet de gebruiker in `users`

Een ingelogde gebruiker staat niet per se in de tabel `users`: wie via `ALLOWED_EMAILS`
of als projectlid toegang heeft, komt er niet in voor. Bevestigt zo iemand een factor,
dan maakt `UserOtpService._row_or_create` een rij voor hem aan, want de OTP-kolommen
moeten ergens staan.

Dat heeft een gevolg voor offboarden. `opi/core/startup.py` laadt bij elke start elk
adres uit `users` in de inloglijst. Iemand uit `ALLOWED_EMAILS` of uit de ledenlijst van
een project halen is daarna dus niet genoeg: zijn rij in `users` laat hem na een herstart
opnieuw in het portaal. Verwijder hem ook op **Gebruikersbeheer** (`/admin/users`), waar
de rij zichtbaar staat. Projectgegevens van anderen krijgt hij er niet mee, want de
projectenlijst en de rechten per project gaan over lidmaatschap en niet over deze lijst.

## Eerst opnieuw inloggen

Een 2-factor inrichten of vervangen vraagt eerst een herlogin bij Keycloak. Zonder die poort kan
iedereen met een open sessie op een ontgrendelde computer een eigen 2-factor instellen onder de
naam van een ander, en daarmee de step-up-handelingen doen (verwijderen op productie, tokens,
platformbeheer). Dit is hoe GitHub (sudo mode) en Google ("verifieer dat jij het bent") het ook
doen. Een bevestigingsmail als poort is zwakker, want wie de computer heeft, heeft meestal ook de
open mail.

De pagina `/account/otp` zegt vooraf dat dit gaat gebeuren, met een knop **Opnieuw inloggen**. Het
formulier om te beginnen verschijnt pas na een geslaagde herlogin.

- `GET /account/otp/herlogin` stuurt naar Keycloak met `prompt=login` en `max_age=0`. Keycloak vraagt
  dan opnieuw om je wachtwoord, ook als je daar nog een sessie hebt.
- `GET /account/otp/herlogin/terug` controleert de id_token: dezelfde `sub` en hetzelfde
  e-mailadres als de ingelogde gebruiker, `email_verified`, en een `auth_time` van hooguit een
  minuut oud. Pas dan opent het venster.
- Het venster is **vijf minuten** en schuift niet mee (`HERLOGIN_VENSTER`). De poort staat op
  `POST /account/otp/start`. `/bevestigen` heeft hem niet: die wordt beschermd door de rij die
  `/start` zet, en een tweede poort zou een registratie vernietigen die je al gescand hebt als het
  venster tijdens het scannen verloopt.
- De state van de herlogin (state, nonce, PKCE-verifier) en de uitkomst staan in de tabel
  `otp_herlogins`. Dat is gebouwd toen de sessie nog in een cookie van ~4 KB zat; sinds RC-390 staat
  de sessie server-side en kan dit ook daarin, zodat de tabel kan vervallen (open vraag). De rij is
  gebonden aan het `login_at` van de sessie waarin de herlogin begon, dus een andere sessie van
  dezelfde gebruiker heeft er niets aan.
  De uurlijkse schoonmaaklus ruimt verlopen rijen op.
- Mislukt de herlogin (verlopen, een ander account, geen `auth_time` in de id_token), dan staat er
  op de pagina dat opnieuw inloggen niet gelukt is, en niets opent.

Vereist: de OPI-client in Keycloak moet `auth_time` in de id_token zetten. Dat doet de
default client scope `basic`, die Keycloak sinds versie 25 aan een nieuwe client meegeeft. Een
client die op een oudere Keycloak is aangemaakt heeft hem niet achteraf gekregen, dus controleer
hem per cluster: `GET /admin/realms/<realm>/clients` moet `basic` in `defaultClientScopes` van de
OPI-client noemen. Ontbreekt hij, dan staat dat in de log van OPI ("de id_token mist auth_time")
en kan niemand een 2-factor inrichten.

Voor een gebruiker zonder eigen wachtwoord bij ons (SSO Rijk) kan de IdP de herlogin stil
afhandelen. Hun 2-factor staat bij SSO Rijk en niet hier.

## Opnieuw inrichten

Een bestaande, bevestigde factor vervangen vraagt eerst een geldige code van die factor.
Zonder die stap zou wie een sessie overneemt ook de 2-factor kunnen overnemen. Wie
die code niet meer kan maken, vraagt een platformbeheerder om een reset. Je eigen factor
registreren of vervangen vraagt verder nooit een verhoogde stap: dat is zelfbediening. De
herlogin hierboven komt daar wel bovenop, ook als je al een factor hebt.

## Waar een gestarte registratie staat

Tussen /account/otp/start en de bevestiging met de code staat de lopende registratie
server-side in de tabel `otp_pending_registrations`: alleen de vingerafdruk van het
AGE-versleutelde zaad uit het verborgen formulierveld, tien minuten geldig, met een
opruiming via de uurlijkse schoonmaaklus van de taakwerker. Niets daarvan staat in de
sessie. De sessiecookie droeg toen de hele inhoud, met al een id_token erin, en
daarbovenop (gezien op 27-10-2026 op de sandbox) viel de browser zo'n cookie geruisloos
weg boven ~4 KB. (Inmiddels staat de hele sessie server-side en draagt de cookie alleen een
ondertekend id, zie `server-side-sessies.md`.)

De eigenlijke oorzaak van de mislukte bevestiging was ook een andere: de seed is AGE-armor met
regeleinden, en een browser stuurt elke LF van een verborgen veld als CRLF terug, dus de
vingerafdruk klopte nooit. `otp_bevestigen` normaliseert dat nu.

Een registratie die verlopen is (tien minuten) maar waarvan de afdruk nog in de tabel staat, toont
dezelfde QR opnieuw met de melding dat hij verlopen was en dat een nieuwe code volstaat
(`renew_expired_pending_registration`). Opnieuw scannen hoeft dan niet. Een seed die niet van een
echte `/start` komt, krijgt die verlenging niet. De fout staat in de pagina zelf en nooit in de URL.

Die verlenging loopt nooit voorbij `PENDING_MAX_AGE` (twintig minuten) na de `/start`. Onbegrensd
verlengen hield een achtergelaten registratiepagina bevestigbaar: wie later aan de ontgrendelde
computer gaat zitten, scant de QR die nog op het scherm staat, krijgt met zijn eerste poging een
verlenging, en richt daarmee een eigen 2-factor in zonder herlogin en zonder een code van de
bestaande factor. Dat is precies de open sessie waar de poort op `/start` tegen is.

## Blokkade na mislukte codes

Vijf mislukte codes achter elkaar blokkeren de factor een kwartier
(`OTP_MAX_FAILED_ATTEMPTS` en `OTP_LOCK_DURATION` in
`opi/services/user_otp_service.py`). Tijdens die blokkade faalt ook een goede code. De
blokkade heft zichzelf op als de tijd verstreken is; eerder weg kan alleen met een reset
door een platformbeheerder. Een geslaagde code zet de teller op nul.

## Herstel

### Je telefoon kwijt

Er is een weg: een platformbeheerder reset je factor, daarna registreer je opnieuw.

1. Vraag een platformbeheerder om een reset. Op **Gebruikersbeheer** (`/admin/users`)
   staat per gebruiker een knop **OTP resetten**.
2. Ga daarna naar `/account/otp`. Daar staat weer "Nog niet ingericht", en richt je je
   factor in op je nieuwe telefoon.

Een platformbeheerder kan zijn eigen factor daar niet resetten: dat zou van elke
overgenomen beheerderssessie een weg om de factor heen maken. Vervangen kan wel, op
`/account/otp`, en dat vraagt een code van de bestaande factor.

### De enige platformbeheerder, zonder tweede beheerder

Is er niemand die de knop voor je kan indrukken, dan is de SQL-procedure hieronder het
noodpad. Die draait rechtstreeks op de database van OPI en laat geen logregel na, dus
noteer wat je deed en waarom.

### Wat een reset leegmaakt

Voor wie dit buiten kantooruren met de hand moet doen. In de rij van de gebruiker in
`users`:

| kolom | waarde na een reset |
|---|---|
| `otp_secret` | `NULL` |
| `otp_key_version` | `1` |
| `otp_confirmed_at` | `NULL` |
| `otp_last_used_step` | `NULL` |
| `otp_failed_attempts` | `0` |
| `otp_locked_until` | `NULL` |

```sql
UPDATE users SET otp_secret = NULL, otp_key_version = 1, otp_confirmed_at = NULL,
                 otp_last_used_step = NULL, otp_failed_attempts = 0, otp_locked_until = NULL
 WHERE lower(email) = 'iemand@rijksoverheid.nl';
```

De `lower()` is geen detail: `users.email` bewaart wat een beheerder typte, terwijl de
inlogdienst het adres kleingeletterd levert. `UserOtpService` zoekt om dezelfde reden
hoofdletterongevoelig.

Een handmatige ingreep laat geen spoor achter waar de knop in het portaal dat wel doet
(een INFO-regel met wie het voor wie deed). Noteer hem dus ergens.

### Later

Een reset via een gemailde link is een mogelijke uitbreiding, nog niet besloten.

## Hoe het is opgeslagen

- **Het TOTP-zaad** staat AGE-versleuteld met de platformsleutel in `otp_secret`, nooit
  plat. Een TOTP-geheim kan niet gehasht worden, want de platte waarde is nodig om de code
  uit te rekenen. `otp_key_version` noteert met welke sleutel, zodat roteren later kan
  zonder te moeten raden.
- **`otp_last_used_step`** is het tijdvenster van de laatst geaccepteerde code. Een stap
  tot en met die waarde wordt geweigerd; zonder die grens blijft een onderschepte code de
  rest van zijn periode geldig.

Deze kolommen hebben **een** eigenaar: `UserOtpService`
(`opi/services/user_otp_service.py`). `tests/test_otp_kolommen_eigenaar.py` bewaakt dat er
geen tweede schrijver bijkomt, want die zou de telling, de blokkade en het hergebruik van
een tijdstap stilletjes omzeilen.

## Configuratie

Geen vlag en geen instelling. De factor is er voor wie hem inricht, en wie dat niet doet
merkt er niets van. Wat je wel nodig hebt is de platformsleutel
(`SOPS_AGE_PUBLIC_KEY`/`SOPS_AGE_PRIVATE_KEY`), zoals voor elk ander versleuteld veld.

## Waar het staat

| bestand | rol |
|---|---|
| `opi/utils/totp.py` | zaad, otpauth-URI, Base32, en `verify_totp` |
| `opi/utils/qr.py` | de QR als inline SVG (segno, server-side) |
| `opi/services/user_otp_service.py` | de enige lezer en schrijver van de OTP-kolommen |
| `opi/services/persistence/users.py` | het `User`-model met de OTP-kolommen |
| `opi/migrations/versions/009_add_user_otp.py` | de migratie |
| `opi/web/router_otp.py` | `/account/otp` en de drie stappen eronder |
| `opi/web/router_user_admin.py` | `POST /admin/users/<id>/otp-reset` |
| `opi/core/step_up.py` | wanneer een handeling een step-up vraagt, en of er een verse is |
| `opi/services/step_up_conditions.py` | de condities die een route bij `@requires_step_up` noemt |
| `opi/core/auth_decorators.py` | de decorator `requires_step_up` |
| `opi/web/router_step_up.py` | `POST /step-up` |
| `opi/web/step_up_dialog.py` | de dialoog en de herhaling erna |
| `opi/api/otp_step_up.py` | de OTP-headers bij een API-aanroep |

## Afhankelijkheden

`segno` (BSD) voor de QR. Server-side en inline, want deze pagina draagt een geheim en
daar hoort geen script van een CDN bij.
