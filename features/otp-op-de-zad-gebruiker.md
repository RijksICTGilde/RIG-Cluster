# OTP op de ZAD-gebruiker

## Wat het is

Elke ZAD-gebruiker kan in het portaal zelf een tweede factor registreren: een TOTP-geheim
dat hij in een authenticator-app zet, zoals Google Authenticator. ZAD verifieert die codes
zelf. Er is geen tweede soort factor en er zijn geen herstelcodes: raak je je app kwijt,
dan reset een platformbeheerder je factor en registreer je opnieuw.

Dit levert alleen de factor en de verificatie op. **Er is nog geen handeling in ZAD die
erom vraagt**; dat komt in `plans/verwijderen-in-productie-vraagt-een-verse-otp.md` en de
verhoogde-rechten-stap uit `plans/otp-en-verhoogde-rechten.md`.

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

## Afhankelijkheden

`segno` (BSD) voor de QR. Server-side en inline, want deze pagina draagt een geheim en
daar hoort geen script van een CDN bij.
