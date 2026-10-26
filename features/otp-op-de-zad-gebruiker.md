# OTP op de ZAD-gebruiker

## Wat het is

Elke ZAD-gebruiker kan in het portaal zelf een tweede factor registreren: een TOTP-geheim
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
| een token uitgeven, roteren of intrekken | altijd (RC-387) |
| een rol wijzigen, een service account toevoegen of verwijderen | altijd (RC-387) |

De zes handelingen van RC-387 wegen de omgeving niet: met een token kan iemand blijvend bij
een project, ook als dat project niet op productie staat. Zie
[leden-en-api-tokens.md](leden-en-api-tokens.md).

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

Een geslaagde step-up dekt vijf minuten (`STEP_UP_WINDOW`). Het venster **schuift niet
mee**: een handeling leest de stempel en zet hem nooit opnieuw, dus vijf minuten na de
code is het voorbij, hoeveel handelingen er intussen ook langskwamen.

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

### Via de API en de CLI

Sinds RC-377 vraagt ook elke wijzigende beheerroute een step-up, ongeacht de omgeving: een
handeling van platformbeheer raakt het hele cluster. Voor de rest geldt: buiten `productie`
verandert er niets: de projectsleutel en de bearer-tokens werken zoals
ze werkten. Raakt de handeling `productie`, dan vraagt ze een verse tweede factor van de
AANROEPER zelf. Eén header, en maar één betekenis:

| header | waarde |
|---|---|
| `X-ZAD-OTP` | een verse code uit de authenticator van de aanroeper |

De code wordt getoetst tegen de factor van de **eigenaar van het token**: een persoonlijk
bearer-token hoort bij een gebruiker, en straks, met het plan voor service accounts, hoort
elk token bij een lid. Er is dus geen manier om de code van iemand anders aan een aanroep
te hangen; de oude header `X-ZAD-OTP-User` bestaat niet meer.

Met de **projectsleutel** (`X-API-Key`) kan zo'n handeling niet. Die hoort bij geen enkele
gebruiker, en een machine heeft geen tweede factor: dat is altijd een 403, ook met een
`X-ZAD-OTP` erbij. De verwijderroutes van de API worden met de projectsleutel afgeschermd,
dus die handelingen lopen via het portaal tot er een persoonlijk token op die routes kan.
De beheerroutes van RC-377 lopen al wel op een persoonlijk token: zie
[platformbeheer-via-de-api.md](platformbeheer-via-de-api.md).

Er is hier geen venster: elk verzoek draagt zijn eigen code, en een hergebruikte code faalt.

```bash
curl -X DELETE "https://zad.rijksapps.nl/api/v2/projects/mijnproject/productie" \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-ZAD-OTP: 123456"
```

#### Het antwoord op een weigering

Een geweigerde aanroep krijgt 403 met een problem-detail (`application/problem+json`)
waarvan `type` op `urn:zad:step-up-required` staat. Daaraan hoort een client te zien dat
hij om een code moet vragen en de aanroep moet herhalen. Een ontbrekend of ongeldig token
blijft 401.

| veld | wat het zegt |
|---|---|
| `action` | welke handeling een step-up vraagt, bijvoorbeeld `deployment-verwijderen` |
| `reason` | waarom, bijvoorbeeld dat de deployment op productie staat |
| `remedy` | wat er moet gebeuren |
| `cause` | waarom er geweigerd werd, als waarde om op te sturen |
| `otp_header` | de header waarin de code hoort |
| `enroll_url` | waar je je factor inricht, alleen bij `factor-ontbreekt` |
| `blocked_until` | wanneer het weer mag, alleen bij `geblokkeerd` |

`cause` onderscheidt vijf gevallen: `otp-ontbreekt`, `factor-ontbreekt` (met
`enroll_url: /account/otp`), `code-ongeldig`, `geblokkeerd` (met `blocked_until`) en
`tokensoort-niet-toegestaan` (de projectsleutel). `code-ongeldig` dekt zowel een verkeerde
als een al gebruikte code: `detail` zegt nooit welk deel van een code fout was.

Elke route met `@requires_step_up` documenteert dit in de OpenAPI: `X-ZAD-OTP` als
optionele header en de 403 met het schema `StepUpProblem`. Dat wordt uit de markering op
de route afgeleid (`custom_openapi` in `opi/server.py`), en
`tests/test_step_up_routes.py` faalt als een step-up-route die documentatie mist.

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

1. Ga naar **Account** (je naam rechtsboven) en kies **Tweede factor inrichten**, of ga
   rechtstreeks naar `/account/otp`.
2. Scan de QR, of voer de Base32-sleutel met de hand in.
3. Vul een code uit je app in. **Pas een geldige code maakt de factor echt**: tot dat moment is er
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

## Opnieuw inrichten

Een bestaande, bevestigde factor vervangen vraagt eerst een geldige code van die factor.
Zonder die stap zou wie een sessie overneemt ook de tweede factor kunnen overnemen. Wie
die code niet meer kan maken, vraagt een platformbeheerder om een reset. Je eigen factor
registreren of vervangen vraagt verder nooit een verhoogde stap: dat is zelfbediening.

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
