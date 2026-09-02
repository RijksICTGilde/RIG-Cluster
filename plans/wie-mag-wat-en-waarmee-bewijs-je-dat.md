# Wie mag wat, en waarmee bewijs je dat

Opdracht: lever een plan van aanpak op voor een degelijk rechtensysteem in ZAD. Drie vragen: **welke rechten kent het platform eigenlijk**, **hoe leggen we vast wie ze heeft**, en **hoe gaan we om met sleutels en tokens, per persoon en per project, ook als de aanroeper een agent is**. Het resultaat is documentatie. Er wordt in deze taak geen productiecode geschreven, geen schema gewijzigd en geen route aangepast.

## Waarom dit nodig is

Er is vandaag geen rechtensysteem. Er zijn vijf manieren om binnen te komen, vier los van elkaar bedachte notities van gezag, vier rollen die samen twee niveaus vormen, en een projectsleutel die alles mag en nooit verloopt. Dat werkt zolang het platform klein is en iedereen elkaar kent. Het houdt geen stand zodra projecten van elkaar gescheiden moeten zijn, zodra iemand achteraf wil weten wie iets deed, en al helemaal niet zodra een agent aan de knoppen zit.

De directe aanleiding om het nu te ontwerpen in plaats van later: de enige agent-vriendelijke ingang die we hebben deelt de projectsleutel uit, en die sleutel opent vijfenvijftig mutatieroutes zonder vervaldatum, zonder rol, zonder intrekking en zonder te zeggen wie hem gebruikte.

## Wat er nu is, gemeten op 22 augustus 2026

Dit is het startpunt, niet het antwoord. **Verifieer elk punt in de code voordat je het overneemt, en vul aan wat hier ontbreekt.**

### Vijf poorten

| Poort | Waar | Routes | Wat hij bewijst |
|---|---|---|---|
| SSO-sessie, `@requires_sso` | `opi/middleware/authorization.py` | 83 | een mens is ingelogd bij Keycloak |
| Projectsleutel `X-API-Key`, `validate_api_token` | `opi/api/endpoint_util.py` | 55 | de aanroeper kent het geheim van dit ene project |
| `ADMIN_API_KEY`, `validate_admin_api_key` | idem | 7 | de aanroeper kent een gedeeld platformgeheim uit de omgeving |
| `MASTER_API_KEY`, `validate_master_api_key` | idem | 4 | idem, een tweede gedeeld geheim |
| SSO-bearer-token, `validate_user_token` | `opi/api/user_token_auth.py` | 2 | een JWT met geverifieerde handtekening, issuer, audience en `email_verified` |

De bearer-weg is verreweg de zorgvuldigste (vaste lijst asymmetrische algoritmes, JWKS met sleutelrotatie, klokspeling, identiteit pas na verificatie) en dekt precies twee routes: het opvragen en het aanmaken van projecten.

### Vier notities van gezag, die niet op elkaar aansluiten

1. **Globale allowlist van e-mailadressen**, `UserService._allowed_emails`, in het geheugen. Gevuld bij startup uit een hardgecodeerde lijst in `opi/core/startup.py`, uit de env-variabele `ALLOWED_EMAILS`, en uit de `users`-tabel. Beantwoordt alleen: mag je überhaupt naar binnen.
2. **Platformbeheerders**, `UserService._platform_admin_emails`, in het geheugen, uit een hardgecodeerde lijst plus env. Een platformbeheerder is lid van elk project met rol `admin` en kan daarmee elke projectsleutel lezen (`opi/services/project_authorization.py`, `features/cli-projecten-opvragen.md`).
3. **Projectrollen in het projectbestand**, `users: [{email, role}]`, enum `admin|owner|member|developer` in `opi/schemas/project_v2.json:79`, standaard `developer`.
4. **`ApproverScope`** in `opi/services/catalog/approval.py:54`: `platform-admin|project-admin|project-member`, wie een aanvraag mag goedkeuren. Een eigen as, met eigen namen, die nergens op de drie andere aansluit.

Er is mogelijk een vijfde, dode notie: `UserService._enrich_user_info` (`opi/services/user_service.py:92-94`) zet `is_admin`, `is_developer` en `is_manager` af uit een `organization.role`-claim uit Keycloak. Zoek uit of iets dat leest, en zeg het als het dood is.

### Vier rollen, twee niveaus

`PROJECT_EDIT_ROLES = ("admin", "owner")` (`opi/services/project_authorization.py:27`). Dus `admin` en `owner` zijn hetzelfde, en `member` en `developer` zijn hetzelfde. De enum belooft een verfijning die niet bestaat, en dat is precies het soort belofte waar later een beveiligingsaanname op wordt gebouwd.

### De gate staat overal apart

Er is één centrale helper, `require_project_edit_access` in `opi/web/project_edit_security.py:42-46`. Daarnaast staat de controle nog twaalf keer met de hand uitgeschreven: negen keer letterlijk `if user_role not in ["admin", "owner"]` in `opi/web/router.py` (regels 395, 427, 467, 506, 540, 603, 642, 692, 740) en drie keer in de dienstencatalogus (`invite/__init__.py:235`, `attachments/__init__.py:267`, `keycloak/__init__.py:99`). `features/futures/form-field-rbac.md` benoemt het gevolg al: vergeet één opslagpad en de gate is daar simpelweg afwezig, zonder dat iets dat opmerkt.

### De projectsleutel

Tweeëndertig alfanumerieke tekens (`opi/utils/api_keys.py`), AGE-versleuteld in het projectbestand. Hij opent alle vijfenvijftig mutatieroutes van dat project, draagt zelf geen rol, kent geen vervaldatum, geen rotatie anders dan het projectbestand bewerken, geen intrekking, en zegt niet wie hem gebruikte. Iedere `admin` of `owner` van het project haalt hem op via `GET /api/v2/projects`. De instelling `USE_UNSAFE_API_KEY` vervangt hem door een vaste waarde uit de settings. Een mislukte poging levert een `logger.warning` en verder niets.

### Twee gedeelde platformgeheimen

`ADMIN_API_KEY` en `MASTER_API_KEY` komen uit de omgeving, zijn statisch, worden gedeeld door iedereen die ze heeft, en dekken samen elf routes waaronder opruim- en reconciliatie-acties.

### Een persoon in de broncode

`opi/core/startup.py` zet rond regel 534 en 559 één hardgecodeerd persoonlijk e-mailadres neer als standaard-allowlist én als standaard-platformbeheerder. Dat is een benoemde superuser in de broncode, en het hoort in de inventaris te staan als bevinding, niet als voetnoot.

### Wat er niet is

Geen rechtencatalogus: rechten bestaan uitsluitend impliciet, als de vraag welk codepad je bereikt. Geen audit van wie wat deed met een sleutel. Geen tokens per persoon, geen scopes, geen vervaltermijnen, geen intrekking, geen delegatie, en geen aparte identiteit voor een agent.

### Wat er wel ligt om op te bouwen

Een `users`-tabel in rig-db (`opi/core/user_schema.py`, alleen `email` en `full_name`, geen rol). Een volwaardig geverifieerde bearer-weg. Keycloak, dat client credentials en token exchange kan. Een v2-API die per project al een rol teruggeeft. En een dienstensysteem dat al met `ApproverScope` werkt met de begrippen die je nodig hebt.

## De vraag achter de vraag

Twee dingen lopen nu door elkaar: **wie mag wat** (rollen en rechten) en **waarmee bewijs je dat** (sleutels en tokens). De projectsleutel maakt ze tot één ding, want wie het geheim heeft *is* het project. Het plan moet die twee uit elkaar trekken, want vrijwel elk probleem hierboven komt uit die versmelting voort.

## Op te leveren documenten

Drie bestanden. Feiten, analyse en aanbeveling gescheiden, zodat een lezer die het oneens is met de aanbeveling de inventaris nog kan gebruiken.

**1. `features/futures/rechten-inventarisatie.md`**

De vijf poorten, de vier notities van gezag en hun vindplaatsen, uitgewerkt en geverifieerd.

Het hart van dit document is **de rechtencatalogus**: welke handelingen kent dit platform, op welk object, op welk niveau (platform, project, deployment, component, dienst). Afgeleid uit de werkelijke routes (de 55 + 7 + 4 + 2 + 83 hierboven), niet uit het hoofd. Per recht: wie mag het vandaag, en waar staat die controle. Groepeer naar object, niet naar router.

Daarna de gaten, elk met vindplaats: routes zonder gate, gates die alleen in de UI zitten en niet in de handler, handelingen waar de sleutel meer mag dan de mens die hem kreeg, en plekken waar dezelfde vraag op twee manieren wordt beantwoord.

**2. `features/futures/rechten-modellen-en-tokens.md`**

De invalshoeken en oplossingsrichtingen, elk met wat hij kost, wat hij oplevert, en waarom hij afvalt of blijft.

*Referentiemodellen naast elkaar.* GitHub (repo-rollen read/triage/write/maintain/admin, organisatierollen, teams; fine-grained PAT's met verplichte vervaldatum, een selectie van repositories en een set permissies per resourcetype, plus goedkeuring door de organisatie; GitHub Apps met kortlevende installatietokens; OIDC-federatie voor Actions zodat er helemaal geen langlevend geheim meer is). Forgejo en Gitea (teams met units en een toegangsmodus per unit, scoped tokens). AWS IAM (principal, action, resource en condition; deny wint altijd; rollen met kortlevende credentials via STS; permission boundaries). Kubernetes RBAC (werkwoorden op resourcetypes, namespace-scoping, en het feit dat ZAD dit zelf al genereert voor gebruikersnamespaces). Per model: wat past hier, wat niet, en waarom. Geen encyclopedie, wel keuzemateriaal.

*Rollen versus rechten.* Vaste rollen met een vaste bundel rechten, of rechten als eerste klasse met rollen als voorgedefinieerde bundels daarbovenop. Zeg wat het kost om die keuze later om te draaien, want dat is de enige reden om er nu over na te denken.

*Waar leggen we het vast.* In het projectbestand, waar de leden nu staan, GitOps-zichtbaar en te reviewen, maar waar elke wijziging een commit en een AGE-hercodering is. Of in de database, waar het geen commit veroorzaakt, maar waar de waarheid over een project dan op twee plekken staat en de ProjectStore niet langer het enige leespad is. Dit is een echte vork met gevolgen voor de rest van de architectuur.

*Tokens.* Per persoon, per project, of allebei. Werk minimaal uit: verplichte vervaldatum of niet, op welk niveau scopes bijten, rotatie zonder onderbreking, intrekking, zichtbaarheid (wie kan een token nog lezen nadat het is uitgegeven, en waarom nu iedere projectbeheerder dat kan), en wat er met de bestaande projectsleutels gebeurt bij invoering. Neem expliciet twee paden mee: Keycloak laten uitgeven (client credentials per project, token exchange voor delegatie) tegenover een eigen tokentabel met gehashte opslag.

*Agents.* Maak het onderscheid dat nu ontbreekt: een agent die **namens een persoon** handelt (gedelegeerd, versmald tot minder dan de persoon zelf mag, kortlevend, en dood zodra die persoon zijn recht verliest) tegenover een agent als **eigen principaal** die aan een project hangt, zoals een GitHub App-installatie of een AWS-rol. Beide zijn legitiem en beantwoorden verschillende vragen. Werk per vorm uit wat het betekent voor attributie in het logboek, voor intrekking, en voor de vraag of een agent ooit meer mag dan de mens die hem startte. Betrek de bestaande CLI-weg (`POST` en `GET /api/v2/projects` op een bearer token, zie `features/cli-project-aanmaken.md` en `features/cli-projecten-opvragen.md`), want dat is vandaag de enige agent-vriendelijke ingang en die geeft juist de allesopenende projectsleutel af.

*AuthZEN, uitgewerkt en niet alleen gewogen.* Dit is de richting waar de opdrachtgever expliciet naar wil kijken, dus behandel hem niet als één optie tussen vele maar werk hem uit tot een concreet inrichtingsvoorstel, met daarnaast een eerlijk oordeel over wat hij niet oplost.

Feiten om mee te werken, te verifieren: de Authorization API 1.0 van de OpenID Foundation is op 11 januari 2026 een Final Specification geworden (81 stemmen voor, 1 tegen, 25 onthoudingen) en is in maart 2026 als Standards Track gepubliceerd; hij standaardiseert het gesprek tussen PEP en PDP (`POST /access/v1/evaluation` met subject, action, resource en context, en een booleaanse decision met optionele reden) en nadrukkelijk **niet** het beleidsmodel; Keycloak heeft sinds mei 2026 experimentele ondersteuning; Logius heeft een AuthZEN NL GOV-profiel in werkversie. Bronnen: <https://openid.net/authorization-api-1-0-final-specification-approved/>, <https://openid.github.io/authzen/>, <https://www.keycloak.org/2026/05/authzen-as-experimental-feature>, <https://logius-standaarden.github.io/authzen-nlgov/>, en de skill `standaarden:ls-iam`.

Wat het inrichtingsvoorstel moet bevatten:

- **De afbeelding van ZAD op het model.** Wat is hier een subject (een mens uit de sessie, een mens uit een bearer-token, een projectsleutel, een agent), wat is een action (de rechtencatalogus uit document 1, dus dit hangt er direct aan), wat is een resource (platform, project, deployment, component, dienst, backup, sleutel) en wat hoort in de context (het cluster, of de aanroeper via de UI of de API komt, en wat er verder nodig blijkt). Doe dat als tabel, niet als proza.
- **Minstens drie uitgewerkte voorbeeldverzoeken met hun antwoord**, gebaseerd op echte handelingen uit deze codebase: een developer die een image probeert bij te werken, een projectsleutel die een deployment verwijdert, en een platformbeheerder op een reconciliatieroute. Laat zien wat er in het verzoek staat en welke beslissing er uit komt, inclusief de reden bij een weigering.
- **Waar het beslispunt draait.** Drie vormen om tegen elkaar te zetten: een beslispunt in het OPI-proces zelf dat alleen de AuthZEN-verzoekvorm aanneemt (externaliseren wordt dan later een transportwissel in plaats van een herontwerp); een apart beslispunt als eigen component (OPA, Cedar, Topaz) met een netwerkhop per beslissing; of Keycloak als beslispunt, wat aantrekkelijk klinkt omdat Keycloak er al staat maar waarvan de ondersteuning experimenteel is en waarvan de beleidsdata hier in het projectbestand woont. Wat kost elk, wat breekt er als het beslispunt onbereikbaar is, en wat betekent dat voor een platform waar elk cluster zijn eigen OPI draait.
- **Waar het beleid vandaan komt.** Het antwoord op de vraag hoort uit het projectbestand te komen, want daar staan de leden. Beschrijf hoe dat bij het beslispunt komt: als policy-informatie die per verzoek wordt opgehaald, of als beleid dat bij elke projectwijziging wordt geladen. Dit is de plek waar een externe PDP het duurst wordt.
- **Wat het niet oplost.** Zonder rechtencatalogus is er niets om te evalueren, dus dit is hoe dan ook een latere stap dan document 1. AuthZEN levert geen rollenmodel, geen tokenbeheer en geen antwoord op de vraag wie een sleutel mag zien. Zeg dat expliciet, zodat niemand de standaard aanziet voor het rechtensysteem.
- **Het migratiepad.** Twaalf handgeschreven rolcontroles en een centrale helper worden niet in een keer een beslispunt. Beschrijf hoe je met een enkel handhavingspunt begint zonder de bestaande controles te breken, en waaraan je merkt dat het klopt.

Raak daarnaast de Authorization Decision Log-werkversie van Logius aan, die sinds april 2026 een OpenTelemetry-vorm heeft en die bij AuthZEN hoort: elke beslissing is een vast te leggen gebeurtenis. **Er loopt een parallelle taak (RC-149) over gebeurtenissen vastleggen en melden.** Verwijs daarnaar voor het logboekdeel en bouw het niet dubbel; beperk je hier tot wat een autorisatiebeslissing extra vraagt bovenop een gewone gebeurtenis.

*Wat hier niet bij hoort.* De rechten van OPI zelf tegenover Keycloak zijn al ontworpen in `features/futures/keycloak-rechten-overdragen.md`. Verwijs ernaar, herhaal het niet. `features/authorization-wall.md` gaat over een toegangsmuur vóór applicaties van gebruikers en is een ander onderwerp; verwar de twee niet.

**3. `features/futures/rechten-plan-van-aanpak.md`**

De aanbevolen richting, met elke afgevallen richting in één zin zodat de keuze navolgbaar blijft. Daarna fasering, waarbij elke fase op zichzelf waarde heeft en apart uitgerold kan worden; de eerste fase mag geen big bang zijn.

Neem in elk geval een fasering op die uitgaat van AuthZEN als doelarchitectuur, met per fase wat er dan gebouwd wordt en wat er zichtbaar beter is geworden. Als de analyse uitkomt op een andere aanbeveling, zet die ernaast en zeg waarom, maar laat de AuthZEN-fasering staan zodat de keuze te vergelijken is in plaats van weggeredeneerd.

Neem een expliciet migratiepad op voor wat er nu staat: de vier rollen naar het nieuwe model, de bestaande projectsleutels, de twee gedeelde platformgeheimen, en het hardgecodeerde adres in `startup.py`. Een project dat vandaag draait mag er niet door omvallen.

Sluit af met de beslissingen die een mens moet nemen voordat er gebouwd wordt, elk met de opties en een aanbeveling, en met de kleinste eerste stap: als er maar één ding gebouwd wordt, wat dan, en waarom dat.

## Randvoorwaarden

- Nederlands, in de toon van de bestaande documenten in `features/`. Geen emoji. Alinea's op één regel, dus geen harde regelafbrekingen midden in een zin.
- Elk feit over de huidige situatie wijst een bestand aan, met regelnummer waar dat helpt. Wat je niet in de code hebt teruggevonden, staat er niet in, of staat er met de vermelding dat het niet geverifieerd is.
- Zelfbedachte namen voor rollen, rechten, scopes, tabellen of endpoints worden gemarkeerd als voorstel. Ze mogen niet ongemerkt doorlopen tot iets wat op een besluit lijkt.
- Dit is beveiligingsgevoelig materiaal. Als een voorstel iets schrijfbaar maakt wat dat nu niet is, of een pad verbreedt, benoem dat dan expliciet als zodanig in plaats van het als verbetering te presenteren. Een gat in het schema kan een slot zijn.
- Raadpleeg de skill `bio` voor wat de BIO2 eist rond toegangsbeveiliging, logische toegang en beheerdersrechten, en verwerk wat van toepassing is. Als de conclusie is dat een eis hier niet geldt, schrijf dan op waarom.
- Geen productiecode, geen schemawijziging, geen migratie. Wel mag `TODO_FUTURE.md` een verwijzing naar de drie documenten krijgen.
- Verwijs naar bestaand werk in plaats van het over te doen: `features/futures/form-field-rbac.md`, `features/futures/keycloak-rechten-overdragen.md`, `features/futures/tenant-isolation-followups.md`, `features/zad-external-user-support.md`, `features/invite-system.md`, `features/keycloak-realm-roles.md`, `features/user-admin-crud.md`, `features/metrics-endpoint-security.md`, `features/cli-project-aanmaken.md`, `features/cli-projecten-opvragen.md`.

## Wanneer dit af is

1. De drie documenten bestaan op de genoemde paden.
2. De rechtencatalogus dekt de vijfenvijftig routes achter de projectsleutel en de elf achter de gedeelde platformgeheimen. Een reviewer die een willekeurige route uit `opi/api/` pakt, vindt de bijbehorende handeling in de catalogus terug.
3. De vier notities van gezag zijn expliciet beslecht: welke blijft, welke verdwijnt, welke smelten samen. Inclusief een uitspraak over `ApproverScope` en over de mogelijk dode `organization.role`-afleiding.
4. Er staat een uitgewerkte aanbeveling over tokens per persoon tegenover per project, inclusief wat er met de bestaande projectsleutels gebeurt.
5. De AuthZEN-inrichting is concreet uitgewerkt: de afbeeldingstabel (subject, action, resource, context), minstens drie voorbeeldverzoeken met antwoord, de drie vormen van beslispunt afgewogen, en een expliciete uitspraak over wat AuthZEN hier niet oplost. Een conclusie dat het geheel of gedeeltelijk afvalt is een geldige uitkomst, mits onderbouwd en mits de uitwerking er staat.
6. Het agentonderscheid (namens een persoon tegenover eigen principaal) staat erin, met gevolgen voor attributie en intrekking.
7. Geen enkel bestand buiten `features/futures/` en `TODO_FUTURE.md` is gewijzigd.
