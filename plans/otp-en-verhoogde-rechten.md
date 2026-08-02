# OTP op de ZAD-gebruiker en verhoogde rechten voor gevoelige acties

Status: ontwerpnotitie, 2 augustus 2026. Niet gebouwd. Aanleiding: gevoelige handelingen (beheerder worden, een deployment van een productieproject verwijderen) gaan nu zonder extra bevestiging, en er is geen tweede factor op een ZAD-gebruiker.

**Besloten vooraf:** de extra controlestap steunt op een eigen OTP dat de gebruiker in ZAD registreert, niet op herauthenticatie via SSO Rijk. ZAD wordt daarmee beheerder van een tweede-factor-geheim, met alles wat daarbij hoort aan herstel.

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

Ze zijn los te bouwen en te releasen, en dat is met opzet: onderdeel 3 is het meest ingrijpend en profiteert ervan dat 1 en 2 al draaien.

1. **OTP op de gebruiker**: registreren, verifiëren, herstellen.
2. **Een productiemarkering op het project**, want zonder dat begrip is er geen regel te schrijven.
3. **Verhoogde rechten**: welke acties een verse OTP-bevestiging vragen, en hoe dat afgedwongen wordt.

## 3. Onderdeel 1: OTP op de gebruiker

**Datamodel.** Drie kolommen op `users`, in een nieuwe Alembic-migratie `005`:

| Kolom | Waarom |
|---|---|
| `otp_secret` | Het geheim, versleuteld opgeslagen. Nooit in platte tekst in de database. |
| `otp_confirmed_at` | Registratie is pas geldig als de gebruiker één keer een correcte code heeft ingevoerd; tot dat moment telt het geheim niet. |
| `otp_last_used_step` | Het tijdvenster van de laatst geaccepteerde code, om hergebruik binnen hetzelfde venster te weigeren. |

Die derde kolom is het verschil tussen een echte tweede factor en een schijnbare: zonder is een onderschepte code dertig seconden lang herbruikbaar.

**Versleuteling.** Het geheim hoort niet leesbaar in de database. ZAD heeft AGE (`utils/age.py`) en gebruikt dat al voor secrets in projectbestanden, dus dat ligt voor de hand. **Open beslissing 1:** AGE met de clustersleutel, of een aparte sleutel voor gebruikersgeheimen zodat een projectsleutel er niet bij kan.

**Verifiëren.** Uitbreiden van `utils/totp.py` met een `verify(secret, code, now)` die het venster berekent, één stap tolerantie geeft en een constante-tijdvergelijking doet. Geen eigen crypto verzinnen; de HMAC-basis komt uit de standaardbibliotheek.

**Registreren.** Een pagina onder het gebruikersmenu: geheim genereren, QR en Base32 tonen (`build_otpauth_uri` en `totp_base32` bestaan al), en pas opslaan als de gebruiker een geldige code invoert. Toon de herstelcodes in diezelfde stap, want daarna zijn ze niet meer te tonen.

**Herstel bij verlies van het toestel.** Dit is het onderdeel dat het vaakst wordt overgeslagen en het vaakst pijn doet. Twee wegen die elkaar aanvullen: eenmalige herstelcodes bij registratie, en een beheerderspad waarmee iemand met de juiste rol de OTP van een ander kan resetten. Dat tweede is zelf een gevoelige actie, dus het valt onder onderdeel 3, en daarmee ontstaat een kip-ei die je bewust moet oplossen: **open beslissing 2**, mag de allereerste beheerder zichzelf zonder OTP inrichten, of is er een noodpad buiten de UI om?

**Verify:** een test die een code verifieert tegen een bekend geheim en tijdstip (RFC 6238 heeft testvectoren), een test dat dezelfde code binnen hetzelfde venster de tweede keer wordt geweigerd, en een test dat een niet-bevestigd geheim geen toegang geeft.

## 4. Onderdeel 2: de productiemarkering

Zonder een begrip van "dit project is echt in gebruik" is er geen regel te formuleren. De cluster kan het niet zijn, zie sectie 1.

**Voorstel:** een veld op het project, niet op de deployment. Een project is productie of niet; per deployment differentiëren maakt de regel moeilijk uit te leggen en de UI onrustig.

**Open beslissing 3:** wat is de vorm? Een boolean `production: true` is het simpelst en het duidelijkst. Een statusveld met meerdere waarden (`ontwikkel`, `acceptatie`, `productie`) is uitdrukkelijker en biedt later ruimte, maar dan moet per waarde vastliggen wat hij betekent, anders wordt het een label zonder gevolg.

**Open beslissing 4:** wie mag die markering zetten en afhalen? Als een projecteigenaar hem zelf kan afhalen, is de bescherming een formaliteit: je zet hem uit, doet je verwijdering, en zet hem terug. Dat pleit ervoor het afhalen zelf een verhoogde actie te maken, of het bij een platformbeheerder te leggen.

Wat het niet is: een deploymentstatus. Slapend, uitgeschakeld en verwijderd-gemarkeerd bestaan al en gaan over de toestand van een deployment, niet over het gewicht van het project.

## 5. Onderdeel 3: verhoogde rechten

**Het model.** Een actie declareert dat hij verhoogde rechten vraagt, en onder welke voorwaarde. De voorwaarde is een functie van de actie en de toestand, niet alleen van de rol. Bijvoorbeeld: een deployment verwijderen vraagt bevestiging alleen als het project als productie gemarkeerd staat; iemand beheerder maken vraagt hem altijd.

**Vorm.** Een decorator naast de bestaande `requires_sso`, in dezelfde stijl, zodat een route zelf declareert wat hij nodig heeft in plaats van dat een centrale lijst dat bijhoudt. Dat past bij hoe deze codebase het elders doet: de service bezit zijn eigen contract.

**De geldigheidsduur is de kern van het ontwerp.** Bij elke klik een code vragen is onwerkbaar en leidt tot gewenning; één keer per sessie is te zwak. Voorstel: een verhoogde sessie die kort geldig is (in de orde van vijf tot vijftien minuten) en aan de sessie hangt, zodat een reeks samenhangende handelingen niet telkens onderbroken wordt. **Open beslissing 5:** hoe lang precies, en verlengt elke verhoogde actie de duur of loopt hij vanaf de eerste bevestiging door?

**Welke acties.** Voorstel als startpunt, waarbij het principe is: onomkeerbaar of rechtenverhogend.

| Actie | Voorwaarde |
|---|---|
| Deployment verwijderen | Alleen bij een productieproject |
| Project verwijderen | Altijd |
| Iemand de rol admin of owner geven | Altijd |
| De productiemarkering afhalen | Altijd (zie open beslissing 4) |
| OTP van een andere gebruiker resetten | Altijd |

**Open beslissing 6:** vallen er ook acties op services onder, bijvoorbeeld het verwijderen van een database of een bijlage? Die zijn onomkeerbaar en raken data, dus het argument geldt, maar het maakt de maatregel breder en de gewenning groter.

**Wat er niet in moet.** Geen tweede rollenstelsel naast het bestaande. De rol bepaalt nog steeds óf je iets mag; de verhoogde stap bepaalt alleen dat je het nu bewust doet. Dat onderscheid moet in de code zichtbaar blijven, anders groeien er twee autorisatiemodellen naast elkaar.

**Een gebruiker zonder OTP.** Als een verhoogde actie een OTP vereist en de gebruiker heeft er geen, dan is het antwoord niet "sta het toe" maar "richt eerst OTP in". **Open beslissing 7:** geldt dat vanaf dag één, of is er een overgangsperiode waarin de eis alleen geldt voor wie al OTP heeft? Dat eerste is veiliger, het tweede voorkomt dat iedereen tegelijk vastloopt.

## 6. Logging

Elke verhoogde actie hoort een spoor te laten dat achteraf te volgen is: wie, wat, voor welk project, en dat de tweede factor is gecontroleerd. Volg de logging-sectie van `instructions/service-review-checklist.md`: één regel op INFO bij de handeling zelf, een WARNING bij een geweigerde bevestiging, en nooit het geheim of de ingevoerde code in de regel.

Een mislukte OTP-poging is bovendien een beveiligingssignaal, geen ruis. Overweeg een teller met een blokkade na herhaald falen, in de geest van de bruteforce-bescherming die Keycloak-realms al aan hebben staan.

## 7. Volgorde en afhankelijkheden

1. **`totp.py` overnemen en uitbreiden met verificatie** (uit `claude/keycloak-realm-admin-otp`, commit `9b1d8c75`). Los te bouwen en te testen tegen de RFC-testvectoren.
2. **Migratie `005` en het gebruikersmodel**, plus registreren en herstellen. Hangt aan 1.
3. **De productiemarkering.** Onafhankelijk van 1 en 2, kan parallel.
4. **De verhoogde-rechten-decorator**, eerst op één actie zodat het mechanisme zich bewijst voordat het overal komt te staan. Hangt aan 2 en 3.
5. **Uitrollen over de overige acties uit de tabel.**

Onderdeel 1 en 3 kunnen tegelijk. Doe 4 pas als 2 in de sandbox is uitgeprobeerd, inclusief het herstelpad, want dat is waar dit soort maatregelen in de praktijk op stuk loopt.

## 8. De open beslissingen op een rij

1. Waarmee wordt het OTP-geheim versleuteld: de clustersleutel via AGE, of een aparte sleutel voor gebruikersgeheimen?
2. Hoe wordt de kip-ei opgelost dat het resetten van andermans OTP zelf een verhoogde actie is?
3. Is de productiemarkering een boolean of een status met meerdere waarden?
4. Wie mag de productiemarkering afhalen, en is dat zelf een verhoogde actie?
5. Hoe lang blijft een verhoogde sessie geldig, en verlengt gebruik hem?
6. Vallen onomkeerbare service-acties (database, bijlage verwijderen) er ook onder?
7. Geldt de OTP-eis vanaf dag één voor iedereen, of eerst alleen voor wie er al een heeft?
