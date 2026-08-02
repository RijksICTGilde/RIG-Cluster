# OTP op de ZAD-gebruiker en verhoogde rechten voor gevoelige acties

Status: ontwerpnotitie, 2 augustus 2026. Niet gebouwd. **Alle zeven openstaande beslissingen zijn genomen op 2 augustus; zie sectie 8. Waar de tekst hieronder nog een voorstel doet, wint sectie 8.** Aanleiding: gevoelige handelingen (beheerder worden, een deployment van een productieproject verwijderen) gaan nu zonder extra bevestiging, en er is geen tweede factor op een ZAD-gebruiker.

**Besloten vooraf:** de extra controlestap steunt op een eigen OTP dat de gebruiker in ZAD registreert, niet op herauthenticatie via SSO Rijk. Geheimen worden bewaard zoals alle andere wachtwoorden vandaag: AGE-versleuteld, in het cluster en in git, want er is nog geen veiliger plek en een uitzondering onthoudt niemand. ZAD wordt daarmee beheerder van een tweede-factor-geheim, met alles wat daarbij hoort aan herstel.

Alle paden zijn relatief aan `operations-manager/python/` tenzij ze met `instructions/`, `features/`, `plans/` of `opi/schemas/` beginnen.

---

## 1. Wat er al is

**Er ligt een werkende OTP-implementatie, maar voor iets anders.** Branch `claude/keycloak-realm-admin-otp`, op 2 augustus veiliggesteld in commit `9b1d8c75` nadat het daar ruim een maand ongecommit stond. Het geeft de automatisch aangemaakte Keycloak realm-admins een gedeeld TOTP-geheim, AGE-versleuteld in het projectbestand, zichtbaar in het portaal als otpauth-URI, Base32-sleutel en QR, achter de vlag `KEYCLOAK_ENFORCE_ADMIN_OTP` (standaard uit). Dat is service-account-beveiliging en staat los van dit plan, maar `opi/utils/totp.py` is herbruikbaar.

Let op de beperking van dat bestand: het kent vier functies (`generate_totp_secret`, `totp_base32`, `build_credential_representation`, `build_otpauth_uri`) en **kan geen code verifiëren**. Dat hoefde ook niet, want daar doet Keycloak de verificatie. Voor OTP op de ZAD-gebruiker doet ZAD de verificatie zelf, dus dat is nieuw werk: de TOTP berekenen over een tijdvenster en vergelijken, met een tolerantie van één stap terug en vooruit voor klokdrift.

**Die branch loopt 255 commits achter op main** en raakt bestanden die deze week nog zijn gewijzigd (`connectors/keycloak.py`, `manager/keycloak_manager.py`). Neem `totp.py` over, niet de branch.

**De gebruikerstabel is smal.** `opi/services/persistence/users.py`: `id`, `email`, `full_name`, `created_at`, `updated_at`, met een index op e-mail. Migraties zijn Alembic, `opi/migrations/versions/`, laatste is `004_add_runs.py`.

**Rollen zijn per project.** `admin`, `owner`, `member`, `developer` (`project_v2.json:152`), opgelost door `get_user_role_for_project(project_name, user_email)` in `services/project_authorization.py:55`. Afdwingen gebeurt met de decorator `requires_sso` (`core/auth_decorators.py:18`) en `AuthorizationMiddleware`.

**Er is geen productiestatus op een project.** En die is niet af te leiden uit de cluster: 115 van de 117 deployments in productie draaien op `odcn-production`, tegen één op `local`. Op cluster onderscheiden zou dus betekenen dat vrijwel elke handeling een extra stap krijgt, wat de maatregel waardeloos maakt door gewenning. Een aparte, bewuste markering is daarom gerechtvaardigd.

## 2. Drie onderdelen, in deze volgorde

Ze zijn los te bouwen en te releasen.

1. **OTP op de gebruiker**: registreren, verifiëren, herstellen.
2. **De projectstatus**: uitgesteld, zie sectie 4. Wordt eigendom van een nog te bouwen promotieservice.
3. **Verhoogde rechten**: begint bij platformbeheer, niet bij destructieve acties.
4. **De Keycloak-kant** (sectie 5b): het service-account voor OPI, OTP op bestaande realm-admins, en de master-realm. Loopt naast 1 tot en met 3 en heeft zijn eigen volgorde.

## 3. Onderdeel 1: OTP op de gebruiker

**Datamodel.** Drie kolommen op `users`, in een nieuwe Alembic-migratie `005`:

| Kolom | Waarom |
|---|---|
| `otp_secret` | Het geheim, versleuteld opgeslagen. Nooit in platte tekst in de database. |
| `otp_confirmed_at` | Registratie is pas geldig als de gebruiker één keer een correcte code heeft ingevoerd; tot dat moment telt het geheim niet. |
| `otp_last_used_step` | Het tijdvenster van de laatst geaccepteerde code, om hergebruik binnen hetzelfde venster te weigeren. |

Die derde kolom is het verschil tussen een echte tweede factor en een schijnbare: zonder is een onderschepte code dertig seconden lang herbruikbaar.

**Versleuteling.** BESLIST (8.1): AGE met de OPI-sleutel, in envelope-vorm, plus een kolom met de sleutelversie zodat roteren later kan zonder migratie-archeologie. Dat kleine veld nu toevoegen is bijna gratis en achteraf lastig.

Dat is ook de gangbare opzet en geen eigen uitvinding: cijfertekst in de opslag, sleutel in de omgeving van de applicatie, zoals `ActiveRecord::Encryption` en Django-veldversleuteling het doen. Let op het verschil met een wachtwoord: een TOTP-geheim kán niet gehasht worden, want je hebt de platte waarde nodig om de code te berekenen. Versleutelen is dus verplicht en sleutelbeheer is het hele probleem. Database-native alternatieven zijn hier ongeschikt: TDE beschermt tegen een gestolen schijf maar niet tegen iemand die een query kan draaien, en `pgcrypto` met de sleutel als queryparameter zet die sleutel in de querylogs.

**Verifiëren.** Uitbreiden van `utils/totp.py` met een `verify(secret, code, now)` die het venster berekent, één stap tolerantie geeft en een constante-tijdvergelijking doet. Geen eigen crypto verzinnen; de HMAC-basis komt uit de standaardbibliotheek.

**Registreren.** Een pagina onder het gebruikersmenu: geheim genereren, QR en Base32 tonen (`build_otpauth_uri` en `totp_base32` bestaan al), en pas opslaan als de gebruiker een geldige code invoert. Toon de herstelcodes in diezelfde stap, want daarna zijn ze niet meer te tonen.

**Herstel bij verlies van het toestel.** BESLIST (8.2): eenmalige herstelcodes bij registratie zijn de primaire weg. Op termijn komt daar een herstel via een gemailde link bij, in dezelfde vorm als fase 2 van de invite-service, zodra de mailserver er is. Tot die tijd is er geen formeel noodpad: wie zowel toestel als codes kwijt is, wordt op verzoek handmatig geholpen door iemand met database- en AGE-toegang.

Twee dingen die daarbij horen. Leg in de feature-documentatie vast wélke kolommen dan geleegd moeten worden, zodat degene die dat buiten kantooruren doet niet hoeft te gokken. En besef dat een handmatige ingreep geen spoor achterlaat waar de rest van het systeem dat wel doet; noteer hem dus ergens.

Een consequentie die bij de gemailde link hoort en die bewust genomen moet worden: herstel via e-mail maakt de tweede factor zo sterk als de mailbox, en omdat de eerste factor SSO Rijk is dat aan datzelfde adres hangt, valt de tweede factor daarmee terug op de eerste. Voor gewone gebruikers is dat aanvaardbaar; voor accounts met verhoogde rechten is een wachttijd met notificatie of een goedkeuringsstap het overwegen waard.

Je eigen OTP instellen is nooit een verhoogde actie: dat is zelfbediening en kan per definitie geen rechtenverhoging zijn.

**Verify:** een test die een code verifieert tegen een bekend geheim en tijdstip (RFC 6238 heeft testvectoren), een test dat dezelfde code binnen hetzelfde venster de tweede keer wordt geweigerd, en een test dat een niet-bevestigd geheim geen toegang geeft.

## 4. Onderdeel 2: de projectstatus (uitgesteld)

**BESLIST (8.3 en 8.4):** er komt een statusveld met een enum op het project en mogelijk ook op de deployment, maar het wordt in dit plan niet gebruikt. Het krijgt pas betekenis via een nog te bouwen promotieservice die deployments of images door een OTAP-pad loodst; die service wordt eigenaar van het veld en bepaalt wie het mag zetten. Het veld mag alvast toegevoegd worden, ongebruikt.

**Gevolg voor dit plan, en dat is een echte inperking:** de regel "een deployment verwijderen vraagt bevestiging als het project productie is" kan nu niet gebouwd worden. De verhoogde-rechten-stap start dus zonder status-afhankelijke regels.

Wat wel blijft staan uit het oorspronkelijke onderzoek: de cluster kan die rol niet overnemen, want 115 van de 117 deployments draaien op `odcn-production`. Onderscheiden op cluster zou vrijwel elke handeling een extra stap geven en de maatregel door gewenning waardeloos maken.

## 5. Onderdeel 3: verhoogde rechten

**Het eerste doel is niet een destructieve actie maar de breedte van je toegang.** BESLIST (8.7): OTP geldt in eerste instantie alleen voor platformbeheer, oftewel het zien van alle projecten. Dat is het breedste recht in het systeem, het raakt een kleine groep, en het is daarmee zowel het waardevolst om af te schermen als het veiligst om mee te beginnen.

Het aangrijpingspunt is klein en precies aan te wijzen. `UserService.is_platform_admin(email)` (`services/user_service.py:279`) toetst tegen de configuratie `ADMIN_EMAILS`, en wordt op twee plekken gebruikt in `services/project_authorization.py`:

- `is_user_authorized_for_project` (regel 37): een platformbeheerder is altijd geautoriseerd.
- `get_user_role_for_project` (regel 57): een platformbeheerder krijgt altijd de rol `admin`.

Met verhoogde rechten worden die twee voorwaardelijk: zonder verse OTP-bevestiging valt een platformbeheerder terug op zijn werkelijke projectlidmaatschap, precies zoals elke andere gebruiker. Dat is een zichtbare gedragswijziging voor die groep en het is de bedoelde.

**Vorm.** Een decorator naast de bestaande `requires_sso` (`core/auth_decorators.py:18`), zodat een actie zelf declareert wat hij nodig heeft in plaats van dat een centrale lijst dat bijhoudt. BESLIST (8.6): dat is niet alleen stijl maar een eis, want later moet per service en per actie te markeren zijn dat er een verhoogde stap nodig is, in een RBAC-achtige vorm die nog niet bestaat. Uitbreiden moet dan een declaratie toevoegen zijn, geen generieke code aanpassen.

**Geldigheidsduur.** BESLIST (8.5): een kort venster van ongeveer vijf minuten vanaf de bevestiging, dat **niet** meeschuift bij gebruik. Een reeks samenhangende handelingen kan dus achter elkaar, maar wie een half uur later nog iets doet bevestigt opnieuw. Voorspelbaar, en het voorkomt dat de verhoogde toestand bij aaneengesloten gebruik onbeperkt openblijft.

**Later, niet nu.** De volgende acties zijn de logische vervolgstap zodra het mechanisme zich bewezen heeft: iemand de rol admin of owner geven, een project verwijderen, de OTP van een ander resetten, en (zodra de promotieservice er is) een deployment van een productieproject verwijderen.

**Uitdrukkelijk niet in de eerste ronde.** BESLIST (8.6): onomkeerbare service-acties zoals een database of een bijlage verwijderen. Die zijn bovendien minder onomkeerbaar dan ze lijken: een bijlage staat in het projectbestand en dus in de git-historie, en voor een database bestaat de backupweg.

**Wat er niet in moet.** Geen tweede rollenstelsel naast het bestaande. De rol bepaalt nog steeds óf je iets mag; de verhoogde stap bepaalt alleen dat je het nu bewust doet. Dat onderscheid moet in de code zichtbaar blijven, anders groeien er twee autorisatiemodellen naast elkaar.

**Een gebruiker zonder OTP.** Omdat de eerste ronde alleen platformbeheer raakt, is de groep klein en overzichtelijk. Een platformbeheerder zonder OTP werkt gewoon door als gewone gebruiker en richt zijn tweede factor in wanneer hij de brede blik nodig heeft.

## 5b. De Keycloak-kant is afgesplitst

Het service-account voor OPI, OTP op de realm-admins van projecten en de master-realm staan nu in `plans/keycloak-otp-en-service-account.md`, met alle beslissingen van 2 augustus erin verwerkt.

Reden voor de splitsing: dat deel gaat over Keycloak-accounts en niet over ZAD-gebruikers, het is los te bouwen en te testen, en het is de voorwaarde voor de rest (zolang OPI met het gedeelde adminwachtwoord inlogt, sluit elke OTP op dat account OPI buiten). Dit plan blijft over OTP op de ZAD-gebruiker en de verhoogde rechten daarop.

Twee verwijzingen die blijven gelden: `opi/utils/totp.py` komt uit hetzelfde veiliggestelde werk, en de scheiding tussen de twee doelen (Keycloak-OTP tegen brute force op de console, ZAD-OTP als persoonsgebonden tweede factor) staat in beide documenten.

## 6. Logging

Elke verhoogde actie hoort een spoor te laten dat achteraf te volgen is: wie, wat, voor welk project, en dat de tweede factor is gecontroleerd. Volg de logging-sectie van `instructions/service-review-checklist.md`: één regel op INFO bij de handeling zelf, een WARNING bij een geweigerde bevestiging, en nooit het geheim of de ingevoerde code in de regel.

Een mislukte OTP-poging is bovendien een beveiligingssignaal, geen ruis. Overweeg een teller met een blokkade na herhaald falen, in de geest van de bruteforce-bescherming die Keycloak-realms al aan hebben staan.

## 7. Volgorde en afhankelijkheden

1. **`totp.py` overnemen en uitbreiden met verificatie** (uit `claude/keycloak-realm-admin-otp`, commit `9b1d8c75`). Los te bouwen en te testen tegen de RFC-testvectoren.
2. **Migratie `005` en het gebruikersmodel**, plus registreren en herstellen. Hangt aan 1.
3. **De verhoogde-rechten-decorator, alleen op platformbeheer.** Hangt aan 2. Twee call-sites in `project_authorization.py`, dus klein genoeg om het mechanisme te bewijzen voordat het ergens anders komt te staan.
4. **Later:** de overige acties (rol wijzigen, project verwijderen, OTP van een ander resetten), en zodra de promotieservice er is de status-afhankelijke deploymentverwijdering.

Doe stap 3 pas als stap 2 in de sandbox is uitgeprobeerd, inclusief het herstelpad, want dat is waar dit soort maatregelen in de praktijk op stuk loopt.

## 8. Genomen beslissingen (2 augustus 2026)

1. **AGE met de OPI-sleutel, in envelope-vorm**, met een sleutelversie in de rij zodat roteren later kan.
2. **Herstelcodes primair; later een gemailde link** (na de mailserver, zoals fase 2 van de invite-service). Tot die tijd geen formeel noodpad: handmatig op verzoek via database- en AGE-toegang. Je eigen OTP instellen vraagt nooit een verhoogde stap.
3. **De projectstatus wordt een enum op project en mogelijk deployment**, maar wordt hier niet gebruikt.
4. **Eigendom van dat veld ligt bij een nog te bouwen promotieservice** die deployments of images door een OTAP-pad loodst. Die bepaalt ook wie de status mag zetten.
5. **Een kort venster van ongeveer vijf minuten dat niet meeschuift** bij gebruik.
6. **Service-acties (database, bijlage verwijderen) vallen er voorlopig niet onder.** Het mechanisme moet wel zo gebouwd worden dat markeren later een declaratie is en geen wijziging in generieke code; een RBAC-achtige markering op services en acties is toekomstig werk.
7. **De eerste en voorlopig enige verhoogde actie is platformbeheer**: alle projecten mogen zien vraagt een verhoogde sessie. Destructieve acties komen later.
