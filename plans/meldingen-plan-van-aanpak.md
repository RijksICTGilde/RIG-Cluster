# Meldingen in ZAD: de kanalen en het plan van aanpak

**Geschreven op**: 22 augustus 2026. Dit is deel 3 van drie. Deel 1 (de eventcatalogus) staat
in `plans/meldingen-inventarisatie.md`, deel 2 (de oplossingsrichtingen en het datamodel) in
`plans/meldingen-oplossingsrichtingen.md`.

Dit deel gaat over de vier kanalen (wat is er nodig, wat ligt er al, wat blokkeert), over het
voorkeurenscherm, en daarna over de fasering en de beslissingen die de opdrachtgever moet
nemen.

**Bijgewerkt op 28 augustus 2026 door RC-161.** De standaardentabel voor de platformbeheerder,
het voorkeurenscherm, de "waarom kreeg ik dit"-regel, de lijst van wat niet uitgezet mag
worden, de verversingsweg en fase 1 zijn gewijzigd. Wat er precies veranderd is en waarom staat
onderaan dit document onder **"Wijzigingslijst RC-161"**. De onderbouwing staat in
`plans/beheer-in-zad-inventarisatie.md` en `plans/beheer-in-zad-plan-van-aanpak.md`.

**Alle namen van tabellen, endpoints en routes hieronder zijn een voorstel**, tenzij er een
codeanker bij staat.

---

## Kanaal 1: de UI

### Wat er nodig is

Drie dingen, en niet meer:

1. **Een teller in de kop.** Het aantal ongelezen meldingen, klikbaar. In de hulpbalk
   rechtsboven, naast het accountmenu, want dat is waar de gebruiker hem verwacht en het is de
   enige plek in de schil die op elke pagina hetzelfde is
   (`opi/templates_lotc/base_lotc.html.j2`, het blok rond `is_ingelogd`).
2. **Een postvak.** Een pagina met de meldingen, nieuwste eerst, gegroepeerd op draad
   (`thread_key` uit het datamodel). Per regel: wat er gebeurde, waar het over ging, hoe lang
   geleden, en waarom je het ziet. Filters op gelezen/ongelezen en op type. Knoppen om één
   melding of alles als gelezen te markeren.
3. **Een weg terug naar het onderwerp.** Elke melding moet ergens heen wijzen: de
   projectdetailpagina, de deploymentkaart, `/admin/approvals`. Dat is geen extra kolom in de
   tabel maar een functie die uit `type` + `project` + `deployment` een pad maakt, want de
   bestemming volgt uit het type en hoort niet per melding opgeslagen te worden (dan is een
   verhuisde pagina een migratie).

### Wat er al ligt

- **De schil en de componenten.** LOTC met het NLDD-thema, `base_lotc.html.j2`,
  `opi/web/navigation_lotc.py` voor de indeling. In gebruik in `opi/templates_lotc/` zijn
  onder meer `c-table`, `c-card`, `c-alert`, `c-icon` en `c-tag`. Wat we hier nodig hebben
  staat al in de bibliotheek zelf (de catalogus, niet het gebruik:
  `lord_of_the_components/templates/components/` en `registry.json`), en dat is meer dan uit
  het gebruik blijkt. Drie dingen die de bouwer moet weten voor hij begint:
  - **De naam `c-notification` is bezet, en niet door ons.** `notification.html.j2` en
    `notification-item.html.j2` bestaan al in de bibliotheek (een `<ul>` met per regel icoon,
    titel, optionele link en een metaregel) en worden gebruikt in
    `opi/templates_lotc/bg/feedback.html.j2:84` als **vluchtige bevestiging** na een actie
    ("Project opgeslagen", "Uitrollen wacht"). Dat is geen postvak: het is de terugkoppeling
    die verschijnt en weer weggaat. Er mag dus geen tweede `c-notification` gebouwd worden.
    Kies bij het bouwen bewust: of je hergebruikt `c-notification-item` letterlijk voor een
    postvakregel (de vorm past), of je geeft de nieuwe component een naam die niet botst
    (voorstel: `c-inbox-item`). Wat niet mag is de bestaande naam overnemen en de betekenis
    stilletjes verschuiven.
  - **`c-activity` en `c-activity-item` staan qua vorm dichter bij een postvakregel dan
    `c-table`.** Een activity-item draagt precies de velden die een melding heeft: actor,
    actie, onderwerp (`res`), tijdstip (`at`) en een link. Een postvak is een lijst met
    regels, geen raster met kolommen; kies de lijstvorm, tenzij de filters op type en
    gelezen/ongelezen een kolomindeling afdwingen.
  - **`c-badge` bestaat wel, maar wordt hier nog nergens gebruikt.** Nul treffers op
    `<c-badge` in `opi/templates_lotc/`; in de catalogus staat hij als "Small count or
    notification badge" met de standen default/info/success/warning/error. De conclusie
    blijft dus dat de teller in de kop een bestaande component is, maar hij is voor dit
    project nieuw: reken op een rondje vormcontrole in de proefopstelling in plaats van
    kopieerwerk van een bestaande pagina.
- **De regels.** `features/lotc-bouwlijn.md`: attributen in kebab-case, samenstellingen
  krijgen kinderen in plaats van data-props, Jinja niet op attribuutpositie. En: nooit een
  `{# ... #}`-commentaar BINNEN een componenttag.
- **Het htmx-patroon voor verversen.** Ligt er in twee vormen, en het verschil tussen die
  twee is voor dit ontwerp belangrijker dan de snelheid. Een lopende taak vervangt zichzelf
  met een kale tijdklok: `hx-trigger="every 2s"`
  (`opi/templates_lotc/partials/task_progress_fragment.html.j2:34`). Dat mag daar, want dat
  venster bestaat alleen zolang de taak loopt. Het blok dat vanzelf bijwerkt gebruikt
  **bewust geen tijdklok**: `opi/templates_lotc/bg/project-tabs.html.j2:987` luistert met
  `hx-trigger="intersect once, zad-metingen-ververs"` op een **eigen gebeurtenis**, en een
  scriptje eronder (`TUSSENPOOS = 60000`, regels 993-1011) vuurt die gebeurtenis elke minuut,
  maar keert meteen terug zolang het tabblad onzichtbaar is
  (`if (document.hidden || typeof htmx === 'undefined') return;`), met daarnaast een haak op
  `visibilitychange` die ververst zodra je terugkomt. Het commentaar op de
  regels 975-986 legt uit waarom die vorm er staat en de kale vorm niet:
  - een htmx-tijdklok blijft doorpeilen als het tabblad naar de achtergrond gaat, dus een
    tabblad dat een dag openstaat bevraagt een dag lang elke minuut de server;
  - en het voor de hand liggende lapmiddel daarvoor, een triggerfilter
    `every 60s [conditie]`, **kan hier niet**: htmx bouwt zo'n conditie met de
    `Function`-constructor en de Content-Security-Policy van deze applicatie staat geen
    `unsafe-eval` toe (`opi/middleware/security_headers.py:56`: `script-src 'self'
    'unsafe-inline' https://cdn.jsdelivr.net`, en `unsafe-eval` staat er niet bij), dus de
    conditie zou stil nooit waar worden. Gemeten in RC-91.
- **De proefopstelling.** `/lotc/bg/<pagina>` met verzonnen gegevens uit
  `opi/web/lotc_fixtures.py`, zodat je vorm kunt kiezen zonder cluster.

**Let op**: de opdracht verwijst naar
`/Users/robbertuittenbroek/IdeaProjects/jinja-roos-components/ROOS_CLAUDE_REFERENCE.md`. Dat
pad bestaat hier niet, en dat klopt: `jinja-roos-components` is sinds RC-67 uit het project
verdwenen (`features/roos-eruit.md`), en `CLAUDE.md` zegt het ook met zoveel woorden ("de
oude ROOS-referentie is met de bibliotheek verdwenen"). De geldende referentie is
`features/lotc-bouwlijn.md` plus `request_for_components.md` voor wat het thema nog niet kan.

### De verversingsweg: peilen vanuit de browser, en niet de websocket of SSE

Er is een websocket-router (`opi/api/logs_websocket_router.py`, 926 regels) met
sessie-authenticatie, Origin-controle, verbindingslimieten en snelheidsbegrenzing. Hij is
goed gebouwd en het is verleidelijk om hem te hergebruiken.

**Doe het niet, en de reden staat in het bestand zelf.** In de kop: "Connection limits are
per-worker. For true global limits across workers, use Redis or a shared state backend." Een
websocket voor logs is een verbinding die iemand bewust opent, kijkt, en sluit. Een teller in
de kop is een verbinding die elke ingelogde gebruiker op elke pagina permanent openhoudt. Dat
is een andere orde: van enkele gelijktijdige verbindingen naar één per open tabblad van
iedereen, met per werker een eigen boekhouding.

**Aanbeveling: peilen vanuit de browser met een minuutcadans, en wel in de
zichtbaarheidsbewuste vorm die er al ligt.** Dus niet `hx-trigger="every 60s"` op de teller,
maar de vorm van `opi/templates_lotc/bg/project-tabs.html.j2:987`: `hx-trigger` op een eigen
gebeurtenis (voorstel: `zad-meldingen-ververs`), een scriptje in de schil met een
`setInterval` van 60000 dat die gebeurtenis vuurt en meteen terugkeert zolang
`document.hidden` waar is, plus een haak op `visibilitychange` die één keer ververst zodra
het tabblad weer op de voorgrond komt.

Waarom de kale tijdklok hier juist niet mag, terwijl hij bij het taakvenster wel mag: de
teller komt in `base_lotc.html.j2`, dus op **elke pagina van elke ingelogde gebruiker in elk
open tabblad**, en hij blijft daar staan zolang die sessie duurt. Dat is precies de last
waarvoor het bestaande blok de kale vorm heeft afgewezen, en dat blok stond nog maar op
één tabblad van één pagina. Een vergeten tabblad met de teller erin zou anders een etmaal lang
1440 verzoeken kosten zonder dat er iemand kijkt. En de vluchtroute die je zou willen nemen,
`every 60s [document.visibilityState === 'visible']`, is er niet: de CSP van deze applicatie
verbiedt `unsafe-eval` en htmx bouwt zo'n conditie met de `Function`-constructor, dus hij
faalt stil (RC-91). Wie dat niet weet bouwt hem, ziet geen fout, en denkt dat het werkt.

Met de zichtbaarheidshaak kost het één `hx-get` per minuut per **zichtbaar** tabblad, het
werkt over meerdere werkers heen zonder gedeelde toestand, en het overleeft een herstart van
OPI zonder dat er iets opnieuw verbonden moet worden. Een melding die een minuut later
binnenkomt is geen probleem: dit is geen chat. En het terugkeergedrag is hier eerder een
voordeel dan een concessie: op het moment dat iemand naar het tabblad terugschakelt staat de
teller meteen goed, in plaats van tot de volgende tik verouderd te zijn.

Voor de postvakpagina zelf mag het sneller (voorstel: 10 seconden) en mag de kale tijdklok
wél, om dezelfde reden als bij `opi/templates_lotc/bg/_tasks.html.j2:28` (daar `every 5s` met
`hx-swap="outerHTML"`): dat is één pagina die iemand bewust openzet, niet iets wat overal
meereist. Wie ook daar netjes wil zijn, hangt er dezelfde zichtbaarheidshaak onder; verplicht
is het niet.

#### De middenweg die hier eerst werd overgeslagen: server-sent events

De vorige versie van deze paragraaf zette websocket tegenover peilen en sloeg SSE over. Dat is
alsnog gewogen, en dan per plek in plaats van in het algemeen, want de drie plekken zijn niet
hetzelfde geval.

**Eerst de metingen, en dan pas de weging.**

| Gegeven | Waarde | Bron |
|---|---|---|
| Aantal OPI-processen | **één** | `replicas: 1` (`bootstrap/rig-system/kustomize/operations-manager/base/deployment.yaml:9`) plus `uvicorn.run(app, host="0.0.0.0", port=8000, loop="asyncio")` zonder `workers=` (`opi/server.py:729`) |
| Wat de CSP toestaat | **een `EventSource` naar dezelfde herkomst mag** | `connect-src 'self'` (`opi/middleware/security_headers.py:60`); `EventSource` valt onder `connect-src`, dus SSE vraagt geen CSP-wijziging |
| Routetijdslimiet op `zad.rijksapp.nl` | **300 seconden** | `haproxy.router.openshift.io/timeout: "300s"` (`bootstrap/rig-system/kustomize/operations-manager/overlays/odcn-production/ingress-rijksapp.yaml:13`) |
| Idem op `operations-manager.rig.prd1.gn2.quattro.rijksapps.nl` | 600 seconden | `bootstrap/rig-system/kustomize/operations-manager/overlays/odcn-production/ingress.yaml:9` |
| SSE in de code vandaag | **bestaat niet** | nul treffers op `EventSource` en op `text/event-stream` in `opi/`; de htmx-SSE-uitbreiding zit niet in `static/js/` (daar staan `htmx.min.js`, `json-enc.js` en eigen scripts) |
| Postgres-verbindingen | 250 | `infrastructure/bootstrap/infrastructure/postgresql/database/base/cluster.yaml:44` |
| asyncpg | ligt er al | `opi/connectors/postgres.py`, `opi/core/database_pools.py` |

**Het bezwaar tegen de websocket, nagemeten.** Dat bezwaar was: "Connection limits are
per-worker" (`opi/api/logs_websocket_router.py:17-19` en `:55`). **Op processen bijt dat vandaag
niet.** OPI draait als één proces met één event loop, dus er is precies één boekhouding en die
is compleet. Dat is de "deels" die uitgezocht moest worden, en het antwoord is: het
per-werker-bezwaar is vandaag geen bezwaar, en het wordt er meteen weer een zodra `replicas` of
`workers` boven één gaat.

**Wat wél bijt, en dat geldt voor de websocket en voor SSE even hard:**

- één event loop houdt N langlopende verbindingen aan, naast het uitrolwerk dat hetzelfde
  proces doet;
- een herstart van OPI verbreekt ze allemaal tegelijk;
- **elke stroom via `zad.rijksapp.nl` wordt na uiterlijk vijf minuten door de router
  afgeknipt.** `EventSource` verbindt daarna zelf opnieuw, dus dat is te overleven, maar het
  betekent twaalf herverbindingen per uur per open tabblad en niet één verbinding die de dag
  doorkomt.

**Waarin SSE wél goedkoper is dan de websocket**: een SSE-stroom is een gewone HTTP-respons.
De sessieauthenticatie, de Origin-controle en de CSP gelden er ongewijzigd voor, dus er is geen
tweede authenticatiepad nodig zoals de websocket-router dat heeft opgebouwd.

**En `LISTEN/NOTIFY` eronder?** Kan, en asyncpg ligt er al. Eén voorwaarde die makkelijk
vergeten wordt: de luisterende verbinding moet **buiten de pool** staan, want een verbinding die
teruggaat naar de pool raakt zijn listener kwijt. Met één OPI-proces kost dat precies één extra
verbinding op een server die er 250 aankan. Dat is dus geen bezwaar. Het bezwaar zit ergens
anders, en dat is per plek verschillend.

**De beslissing per plek.**

| Plek | Uitkomst |
|---|---|
| De teller in de kop | **peilen, één keer per minuut, zichtbaarheidsbewust** (ongewijzigd) |
| De postvakpagina | **peilen, 10 seconden** (ongewijzigd) |
| Het beheerdersoverzicht `/beheer` | **peilen, per blok een eigen cadans** |

*De teller in de kop.* Een SSE-stroom per open tabblad is dezelfde soort last als een
websocket per open tabblad, alleen goedkoper per bericht. Voor een teller die een minuut mag
achterlopen levert die stroom niets op wat de peiling niet ook geeft, en hij kost drie dingen
die de peiling niet kost: een openstaande verbinding per tabblad, een herverbindingslus die om
de vijf minuten afgaat, en een cursor (`Last-Event-ID` tegen `notification_deliveries.created_at`)
om te voorkomen dat een herstart meldingen overslaat. Dat laatste is het echte verschil: een
peiling vraagt een **stand** op en heeft daarom geen enkel probleem met een herstart, een stroom
levert **gebeurtenissen** en moet dus weten waar hij gebleven was.

*De postvakpagina.* Hier is SSE het meest verdedigbaar: één pagina, bewust opengezet, iemand
kijkt ernaar. En toch nee. Het verschil tussen "binnen een seconde" en "binnen tien seconden" op
een pagina waar je zelf naar zit te kijken is niet waarneembaar, en dit zou het enige
SSE-endpoint in de hele applicatie zijn. Een mechanisme voor één pagina is een mechanisme dat
niemand onderhoudt.

*Het beheerdersoverzicht.* Hier zou een muurscherm de meeste baat hebben, en toch is het
antwoord hier het duidelijkst nee, en wel om een reden die niets met last te maken heeft:
**het overzicht leest de brontoestand en niet de meldingentabel**
(`plans/beheer-in-zad-plan-van-aanpak.md`, deel 3). Er is dus geen tabel om `LISTEN/NOTIFY` op
te zetten die zijn blokken voedt. De gezondheid komt van ArgoCD (bevraagd, met een cache van 15
seconden, `opi/services/argocd_overview.py:44`), de aanvragen komen uit de projectbestanden in
de `ProjectStore`, en de markeringen uit `marked_for_deletion`. Geen van die drie duwt. Een
SSE-stroom zou daar dus een peiling aan de serverkant zijn met een stroom aan de browserkant:
dezelfde bevragingen, plus een verbinding.

**De uitkomst is dus: peilen blijft, ook hier.** Dat is nu een beslissing met een reden, en geen
overgeslagen alternatief.

**Wat de rekensom zou omdraaien**, zodat dit later te herwegen is zonder de hele afweging
opnieuw te doen:

1. **`replicas` of `workers` boven één.** Dan komt het per-werker-bezwaar terug, en dan is een
   gedeelde bron (Redis, of juist `LISTEN/NOTIFY`) nodig voor elke vorm van duwen.
2. **Een bron die zelf duwt.** Komt de gezondheidstoestand ooit uit een tabel die OPI zelf
   schrijft, in plaats van uit een bevraging bij ArgoCD, dan heeft `LISTEN/NOTIFY` iets om op te
   luisteren en verandert het antwoord voor het overzicht.
3. **Een hogere routetijdslimiet.** De 300 seconden op `zad.rijksapp.nl` is de bindende
   beperking en het is een annotatie, dus hij is te veranderen. Zonder die verandering is elke
   langlopende verbinding daar een verbinding van vijf minuten.

### Wat blokkeert

Niets fundamenteels. Twee dingen om op te letten:

- **De teller staat op elke pagina**, dus de bevraging erachter moet goedkoop zijn. Dat is
  precies waarvoor de partiële index `idx_notification_deliveries_unread` in het datamodel
  staat.
- **De proefopstelling `/lotc/*` is publiek** en zit in de release-image
  (`features/lotc-bouwlijn.md`). Een postvakfixture mag dus uitsluitend zichtbaar verzonnen
  waarden dragen, geen echte projectnamen en zeker geen echte e-mailadressen.

---

## Kanaal 2: de API

### Wat er nodig is

In de vorm van `opi/api/v2`, met getypeerde Pydantic-modellen zoals in `opi/api/task_models.py`
en `opi/api/v2/models.py`. Voorstel:

| Methode en pad (voorstel) | Wat het doet |
|---|---|
| `GET /api/v2/notifications` | het postvak van de aanroeper; filters op `unread`, `category`, `project`, `since`; paginering |
| `GET /api/v2/notifications/unread-count` | alleen het getal, want dat is de vaakste vraag en die hoort niet de hele lijst op te halen |
| `POST /api/v2/notifications/{id}/read` | één melding als gelezen markeren |
| `POST /api/v2/notifications/read-all` | alles als gelezen markeren, met dezelfde filters als de lijst |
| `GET /api/v2/notifications/preferences` | de voorkeuren van de aanroeper, inclusief de standaard van zijn rol voor wat hij niet zelf heeft gezet |
| `PUT /api/v2/notifications/preferences` | de voorkeuren schrijven |

**Autorisatie.** Alle zes gaan over "de aanroeper", en dat is nieuw in deze API. De
projectroutes in `opi/api/v2/router.py` autoriseren op projectlidmaatschap of op de
`X-API-Key` van het project. Die sleutel identificeert een PROJECT, geen mens, dus hij kan
hier niet werken: een postvak dat op een projectsleutel opvraagbaar is, geeft de meldingen van
een persoon aan iedereen die die sleutel heeft.

**De juiste weg staat er al.** `opi/api/user_token_auth.py` is precies hiervoor gebouwd, en de
docstring zegt het zelf: "The rest of this API authenticates per project: an X-API-Key that
belongs to one project and can do nothing outside it." Het is een bearer-token uit de SSO, met
handtekening, uitgever, doelgroep en vervaldatum geverifieerd, en het levert een IDENTITEIT.
De web-UI gebruikt daarnaast de sessie. Die twee zijn de authenticatie voor dit kanaal; de
projectsleutel is dat expliciet niet.

### Wat er al ligt

- **De vorm.** `opi/api/v2/models.py` en `opi/api/task_models.py` laten precies zien hoe een
  antwoordmodel er hier uitziet, inclusief `StrEnum`-velden en generieke responses
  (`TaskResponse[TResult]`, `opi/api/task_models.py:416`).
- **De persoonsgebonden authenticatie.** `opi/api/user_token_auth.py`.
- **De OpenAPI-documentatie is zelfbeschrijvend** en wordt op `/openapi.json` geserveerd, dus
  de zad-cli en een agent zien nieuwe endpoints vanzelf.

### Wat blokkeert

Niets. Wel een keuze die nu gemaakt moet worden.

### De uitgaande weg: expliciet buiten scope, maar niet dichtgemetseld

Een webhook of abonnement waarmee een ANDER systeem onze gebeurtenissen ontvangt, hoort **niet**
in dit traject. Redenen: er is geen afnemer, de Abonneren-standaard van Logius is nog een
werkversie (v0.0.1, niet vastgesteld), en elke uitgaande verbinding vanaf dit cluster is een
netwerkbeleidsgesprek.

Wat wel nu geregeld wordt en niets kost: **het gebeurtenisrecord draagt de CloudEvents-vorm**
(zie deel 2, richting D). Daarmee is een uitgaande weg later een endpoint dat rijen serialiseert,
en geen verbouwing van de opslag.

Wat wel meteen zinnig is en veel goedkoper: **een INKOMEND endpoint.** Een gebeurtenis van
buiten (een GitHub Action die een scanbevinding meldt, een monitoringsysteem) die als melding
in ZAD landt. Dat is één route die een CloudEvent aanneemt, valideert en als gebeurtenis
wegschrijft. Het maakt drie regels uit de inventarisatie die vandaag "bestaat nog niet" zijn
(scanbevindingen, image-veroudering, onderhoud) haalbaar zonder dat ZAD er iets voor hoeft waar
te nemen. Zie de fasering, fase 4.

---

## Kanaal 3: e-mail

**Dit is de valkuil van deze opdracht, en hij is anders dan de opdracht vermoedt.**

### Wat er nodig is

Een verzendweg: OPI moet een SMTP-bericht kunnen aanbieden aan de relay.

### Wat er al ligt, en dat is meer dan verwacht

De opdracht stelt dat OPI vandaag zelf geen mail verstuurt, en dat klopt: er is **nergens in
`opi/` een import van `smtplib` of `aiosmtplib`**. Gemeten met een grep over de hele
pakketboom. `opi/connectors/mail.py` praat uitsluitend met de beheer-API van de relay
(principals aanmaken, limieten zetten, afzendernamen schrijven) en verstuurt niets.

Maar de opdracht stelt ook dat "het platform als klant van zijn eigen dienst met een eigen
account op de relay voor de hand ligt". **Dat account bestaat al.** Dit is de belangrijkste
vondst van dit deel:

| Wat | Waar | Stand |
|---|---|---|
| Het accountnaam-instelling | `MAIL_PLATFORM_ACCOUNT = "zad-platform"`, `opi/core/config.py:407` | bestaat |
| Het dagbudget | `MAIL_PLATFORM_MESSAGES_PER_DAY = 2000`, `opi/core/config.py:408` | bestaat |
| Het geheim met de inloggegevens | `MAIL_PLATFORM_SECRET_NAME = "zad-platform-mail-account"`, `opi/core/config.py:413` | bestaat |
| Het aanmaken bij het opstarten | `MailManager.ensure_platform_account()`, `opi/manager/mail_manager.py:248`, aangeroepen via `ensure_platform_mail_account()` in `opi/core/startup.py:405` | bestaat, niet-kritiek bij het opstarten |
| Het wachtwoord | wordt gegenereerd bij de eerste ontmoeting met een draaiende relay en bewaard in een Secret in de eigen namespace | bestaat, idempotent |
| Het netwerkpad | egressregel naar `rig-prd-ron` op poort 587, `bootstrap/rig-system/kustomize/operations-manager/overlays/odcn-production/network-policy.yaml` | bestaat |

De regel in dat netwerkbeleid draagt zelfs het commentaar: *"587 om zelf post aan te bieden
(ZAD verstuurt uitnodigingen en meldingen als gewoon account)"*. En de docstring bij
`ensure_platform_mail_account` zegt: *"What it does block is password reset and invite mail"*.

**Wat er dus ontbreekt is precies één ding: de SMTP-client.** Een module die de inloggegevens
uit het Secret leest, verbinding maakt met de relay op 587, authenticeert en een bericht
aanbiedt. Dat is klein werk, en het is precies het soort werk waar de connectorregel van dit
project op van toepassing is: het hoort een connector te worden
(`opi/connectors/`) en geen losse aanroep ergens in een service.

### De afzender is een identiteitsbeslissing en geen instelling

De relay dwingt de `From:` af. Voor een project wordt dat
`noreply-rijksapp+<project>@rijksoverheid.nl` (`generate_mail_sender_address`,
`opi/utils/naming.py:742`), inclusief de weergavenaam uit de projectconfiguratie.

Voor het platformaccount is dat bewust anders. `ensure_platform_account` zet het adres op
`MailManager._sender_address(cluster, None)`: het **kale** adres, dus
`noreply-rijksapp@rijksoverheid.nl`, en `from_name=""`, dus **zonder weergavenaam**. Het
commentaar erbij: "ZAD is not a project, so there is no project name to put in the plus part
and no project configuration to take a display name from."

**Gevolg, en dit is de beslissing die de opdrachtgever moet nemen.** Een melding van ZAD komt
aan als een berichtje van `noreply-rijksapp@rijksoverheid.nl`, zonder naam ervoor. Dat is
technisch juist en menselijk mager: de ontvanger ziet niet dat het van het ZAD-portaal komt tot
hij het onderwerp leest. Drie opties:

1. **Laten staan.** Nul werk. De herkenning moet uit het onderwerp komen ("ZAD: je deployment
   ...").
2. **Een weergavenaam zetten voor het platformaccount.** De machinerie bestaat al
   (`set_sender_name`, `opi/connectors/mail.py:269`), dus dit is één regel: het platformaccount
   krijgt `from_name = "ZAD"` of "Rijksapps ZAD". Het adres blijft het kale adres.
3. **Een eigen adres voor het platform.** Bijvoorbeeld `noreply-rijksapp+zad@...`. Dat is een
   afspraak met het mailteam en het botst met de plusdeel-conventie die "plusdeel = project"
   betekent (`opi/manager/mail_manager.py:56`, `MailAccountNameError`, dat de botsing tussen
   platform- en projectnaamruimte uitdrukkelijk weert).

**Aanbeveling: optie 2.** Een weergavenaam, geen nieuw adres. Herkenbaar in de postbus, geen
gesprek met het mailteam, en de naamruimteregel blijft intact.

### Wat blokkeert, en dit is het echte werk

Vier dingen, alle vier al gemeten en vastgelegd in `plans/mail-vervolgpunten.md`. Ze staan hier
omdat ze de bruikbaarheid van dit kanaal bepalen, niet omdat dit traject ze moet oplossen.

1. **De relay staat op productie nog niet aan.** Punt 5 van `mail-vervolgpunten.md`: de
   manifesten en geheimen zijn klaar, wat rest zijn afspraken en twee regels configuratie
   (`MAIL_RELAY_API_URL` aan in de OPI-overlay, de Application `ron-infrastructure` aan). Zonder
   dat is `MAIL_RELAY_API_URL` leeg, en dan maakt `ensure_platform_account` netjes geen account
   aan en meldt het in het log.
2. **De upstream weigert ontvangers buiten `rijksoverheid.nl`.** Punt 8, gemeten op 21 augustus
   2026: naar een adres bij rijksoverheid.nl volgde `250 ok`, naar een gmail-adres `550 #5.1.0
   Address rejected` op de RCPT TO. **Dat raakt dit traject direct.** In
   `bootstrap/rig-system/kustomize/operations-manager/overlays/odcn-production/configmap.yaml:45`
   staat vandaag een `ALLOWED_EMAILS` met zes adressen, waarvan er één op `odc-noord.nl` staat.
   Die persoon is met de huidige afspraak per e-mail niet te bereiken. Een meldingssysteem dat
   ervan uitgaat dat mail werkt voor iedereen, klopt dus niet.
3. **Bounces verdwijnen stil.** Punt 10: mislukt een bezorging, dan maakt de relay een DSN en
   stuurt die naar het envelope-adres, en de upstream weigert dat adres ALS ONTVANGER met
   dezelfde 550. De relay noteert "discarding message after double bounce" en gooit hem weg.
   **Voor meldingen betekent dat: het kanaal kan zeggen dat het is gelukt terwijl het bericht
   nergens aankwam.** Wat de outbox als `sent` markeert, is "de relay heeft hem aangenomen", en
   dat is niet hetzelfde als afgeleverd. Dat moet in de UI ook zo staan, en niet als "verstuurd".
4. **De MTA-STS-lookup kost twee minuten per bericht naar een domein dat het publiceert.** Punt
   9, gemeten: 131 seconden naar gmail.com. Raakt ons alleen als er ooit buiten
   `rijksoverheid.nl` gemaild wordt, en dat kan vandaag toch niet. Wel relevant voor de
   time-out van de outboxplanner: die moet royaal zijn, anders markeert hij een bezorging als
   mislukt terwijl hij nog loopt.

**Conclusie voor de fasering: e-mail is niet fase 1.** Het account is er, het netwerkpad is er,
de client is klein werk. Maar het kanaal is pas eerlijk als de relay op productie aanstaat en
als duidelijk is wat er met een niet-bezorgbaar adres gebeurt. Tot die tijd is het postvak het
kanaal, en dat werkt voor iedereen.

### Afmelden, samenvatten, en hoeveel er in een mail hoort

**Afmelden.** Elke mail draagt een regel met een link naar het voorkeurenscherm. Geen
`List-Unsubscribe`-header met een eigen afmeldweg: die is bedoeld voor bulkpost aan mensen die
zich ooit aanmeldden, en dit is werkverkeer aan collega's. De weg naar "zet dit type uit" is
het scherm, en dat scherm is één klik ver.

**Samenvatting versus meteen.** Beide, per type instelbaar, met een bruikbare standaard:

- **Meteen** voor wat een handeling vraagt: een storing, een aanvraag die op jou wacht, een
  onomkeerbare ingreep.
- **Dagelijkse samenvatting** voor de rest. Eén mail per dag met wat er die dag is bijgekomen.
  Dat is ook de rem op het uitbarstingsprobleem: twintig gebeurtenissen op een slechte middag
  worden één mail.

De samenvatting is in het datamodel goedkoop: de outboxrijen krijgen `next_attempt_at` op het
verzendmoment van de samenvatting in plaats van op nu, en de verzender bundelt wat voor
dezelfde ontvanger klaarstaat. Geen tweede mechanisme.

**Hoeveel inhoud.** De aanname in de opdracht is: zo min mogelijk, met een link terug, want de
mail verlaat ons vertrouwensgebied. **Die aanname klopt en ik weerleg hem niet, maar hij is te
streng als je hem letterlijk neemt.** Een mail met alleen "er is iets gebeurd, klik hier" is
onbruikbaar: de ontvanger moet kunnen beslissen of hij nu moet handelen of pas maandag, en
daarvoor moet hij weten wat er gebeurde en waarover.

Voorstel voor de grens:

| Wel in de mail | Niet in de mail |
|---|---|
| wat er gebeurde, in één zin | logregels, stacktraces, foutmeldingen van de applicatie |
| het projectnaam en de deploymentnaam | omgevingsvariabelen, geheimen, verbindingsgegevens |
| de ernst en het tijdstip | e-mailadressen van andere betrokkenen |
| waarom je hem krijgt | de inhoud van het projectbestand |
| een link naar het onderwerp in ZAD | alles waarvoor je in ZAD moet zijn ingelogd om het te zien |

De onderliggende regel: **wat in de mail staat, is wat er in een openbaar berichtenlogboek zou
mogen staan.** Een projectnaam mag dat; een foutmelding uit een container niet, want die kan
alles bevatten. Dat is dezelfde regel als het CloudEvents-profiel op de context-attributen
legt (deel 2, richting D), en het is prettig dat die twee samenvallen.

---

## Kanaal 4: Mattermost

### Wat er nodig is

Vier dingen, en het derde is het echte werk:

1. **Een bereikbare Mattermost**, en dus het antwoord op de vraag of hij op het internet staat
   of achter het Rijksnetwerk.
2. **Een bot met een token**, in een Secret in de namespace van OPI, en een connector die er
   privéberichten mee stuurt (`opi/connectors/mattermost.py`, voorstel, want elke externe
   aanroep gaat in dit project door een connector).
3. **Een koppeling tussen een ZAD-gebruiker en een Mattermost-account**, met bewijs dat de
   persoon werkelijk over dat account beschikt.
4. **Een netwerkregel**, of een tussenstation als de Mattermost achter het Rijksnetwerk staat.

### Wat er al ligt: niets

Een grep over de hele repository op `mattermost`, hoofdletterongevoelig, over Python, Markdown,
YAML en Jinja: **nul treffers.** Er is geen connector, geen instelling, geen netwerkregel, geen
notitie en geen post-mortem. Dit kanaal begint bij nul.

### Wat blokkeert: welke Mattermost is dit, en kunnen we erbij

**Dat is niet uit de code te beantwoorden en het is de eerste vraag die beantwoord moet worden**,
want het antwoord bepaalt of dit kanaal überhaupt kan.

Wat wel vaststaat over het netwerk, en dat is genoeg om de vraag scherp te stellen:

- De namespace van OPI op productie draagt `egress.projectcalico.org/egressGatewayPolicy:
  "internet"` (`bootstrap/rig-system/kustomize/overlays/odcn-production/namespace.yaml:7`).
- Het netwerkbeleid van OPI laat uitgaand verkeer toe op poort 443 naar elke bestemming
  (`.../operations-manager/overlays/odcn-production/network-policy.yaml`).

**Dus: een Mattermost op het internet is bereikbaar.** Zonder nieuwe regel, zonder gesprek.

**En een Mattermost binnen het Rijksnetwerk is dat niet, en dat is een harde blokkade.**
`docs/ron-koppeling.md:70`: de annotatie neemt **één** waarde, `internet` of een klantgateway
zoals `rig-ron`, en de een vervangt de ander. `plans/mailrelay.md:258` heeft dat gemeten en
getrokken tot de conclusie: RON aanzetten op `rig-prd-operations` kost daar het internet, en
daarmee ArgoCD, de registry en Keycloak. Precies daarom draait de mailrelay in een EIGEN
namespace (`rig-prd-ron`) en niet naast OPI.

Als de Mattermost op RON staat, is de oplossing dus dezelfde als bij de mail: een klein
tussenstation in een eigen namespace met `rig-ron`, waar OPI intern naartoe praat. Dat is geen
regel erbij, dat is een component erbij.

**Wat de opdrachtgever moet uitzoeken, in deze volgorde:**

1. Welke Mattermost is het (een URL)?
2. Staat die op het internet of achter het Rijksnetwerk?
3. Mogen wij er een bot registreren, en wie beheert dat token?

Vraag 2 is de blokkade; de andere twee zijn afspraken.

### Het echte vraagstuk is niet versturen maar koppelen

Versturen is een POST. **De vraag is: hoe weet ZAD welk Mattermost-account bij een persoon
hoort?** ZAD kent mensen op e-mailadres (uit Keycloak, uit de `users:`-lijst van een project,
uit `ALLOWED_EMAILS`). Mattermost kent mensen op gebruikersnaam en op een eigen id.

Drie manieren, en de derde is de enige die schaalt:

1. **Zoeken op e-mailadres via de Mattermost-API.** De bot vraagt "welke gebruiker heeft dit
   adres". Werkt als de adressen overeenkomen. Nadeel: het vraagt een bot met leesrechten op de
   gebruikerslijst van de hele werkruimte, en dat is een stevig recht om te vragen voor een
   meldingsfunctie. En het faalt stil bij iemand die daar een ander adres gebruikt.
2. **De gebruiker vult zijn Mattermost-naam in het voorkeurenscherm in.** Eerlijk, maar
   onverifieerbaar: hij kan die van een ander invullen en dan gaan zijn meldingen daarheen.
3. **De gebruiker koppelt zichzelf, met bewijs.** ZAD toont een code, de gebruiker stuurt die
   in een privébericht naar de bot, de bot meldt het terug aan ZAD, en de koppeling staat.
   Omgekeerd kan ook: de bot stuurt de code, de gebruiker plakt hem in ZAD. Dat is hoe elke
   koppeling van dit type werkt, en het is de enige vorm waarin ZAD weet dat de persoon
   werkelijk over dat account beschikt.

**Aanbeveling: 3, en dat betekent een tabel `notification_channel_identities` (voorstel) met
`recipient`, `channel`, `external_id` en `verified_at`.** Die tabel staat bewust niet in het
datamodel van deel 2: hij hoort bij dit kanaal en niet bij de kern.

### Een kanaalwebhook is geen persoonlijke melding

Dit moet expliciet, want het is de goedkope oplossing die zich als de echte voordoet.

Een **inkomende webhook** in Mattermost is een URL waar je een bericht naartoe POST, en dat
bericht komt in een KANAAL. Dat is:

- geen persoonlijke melding (iedereen in het kanaal ziet alles van iedereen);
- niet te filteren met de voorkeuren van een persoon (er is geen persoon);
- een informatielek zodra de meldingen projectgegevens dragen en het kanaal breder is dan het
  project;
- wel binnen een half uur werkend.

Een **bot met een privébericht** is de vorm die de wens beschrijft: persoonlijk, per persoon
in te stellen, en niet zichtbaar voor anderen. Het kost een botregistratie, een token in een
Secret, en de koppeling hierboven.

**Aanbeveling: allebei, maar niet als alternatieven van elkaar.** De bot is het persoonlijke
kanaal en hoort bij dit traject. De kanaalwebhook is iets anders: een PROJECTinstelling ("stuur
meldingen over dit project ook naar ons teamkanaal"), waar het projectteam zelf zijn webhook-URL
invult. Dat is een dienst in de catalogus en geen persoonlijk kanaal, en het is een prima
tweede stap. Ze moeten alleen nooit in hetzelfde voorkeurenscherm terechtkomen, want dan gaat
iemand ervan uit dat zijn persoonlijke instelling ook geldt voor wat er in het teamkanaal
verschijnt.

---

## De voorkeuren

### Het scherm

Op `/account/meldingen` (voorstel), naast de bestaande accountpagina. Een tabel: de twaalf
typen uit deel 1 als rijen, de kanalen als kolommen, een aanvinkvakje per snijpunt.

```
                                    Postvak    E-mail    Mattermost
Uitrol van een deployment              x         x           .
Verwijderingen                         x         x           .
Gezondheid van een deployment          x         x           .
Ingrepen door het platform             x         x           .
Backups en gegevens                    x         x           .
Aanvraag wacht op mij                  x         x           .
Besluit over mijn aanvraag             x         x           .
Leden en toegang                       x         x           .
Wijzigingen aan diensten               x         .           .
Werkomgevingen                         x         .           .
Mededelingen van het platform          x         x           .
```

Boven de tabel: welke rol je hebt en dus welk standaardprofiel je krijgt, met een knop om
terug te zetten naar de standaard. Onder de tabel: de koppeling met Mattermost, als dat kanaal
er is.

**Twaalf rijen keer drie kolommen is 36 vakjes.** Dat is veel, en het is de prijs van "per type
per kanaal". De rem erop is dat niemand het scherm hoeft te openen: de standaarden per rol
kloppen, en wie ze nooit aanraakt krijgt iets bruikbaars.

**Voor een platformbeheerder is het hetzelfde scherm, met één zin erboven.** Hij is voor zijn
eigen projecten een gewone projectbeheerder, dus alle twaalf rijen gelden voor hem, alleen niet
voor de 47 projecten van anderen. Dat verschil hoort niet in een extra kolom of een tweede
tabel maar in één regel boven de tabel, voorstel:

> Als platformbeheerder krijg je gebeurtenissen van projecten waar je zelf geen lid van bent
> niet in je postvak. Die staan op het beheerdersoverzicht.

Met een link naar dat overzicht erachter. Een tweede tabel voor de beheerder zou suggereren dat
hij twee verzamelingen voorkeuren heeft, en dat is niet zo: hij heeft er één, en de regel die
zijn beheerdersrol betreft staat niet in een vinkje maar in het uitwaaieren.

### De standaarden per rol

Uit de tabel in deel 1, samengevat, en voor de platformbeheerder herzien door RC-161:

| Rol | Postvak | E-mail | Redenering |
|---|---|---|---|
| **Platformbeheerder**, in die hoedanigheid | **alleen type 6 (een aanvraag wacht op mij) en wijzigingen aan zijn eigen bevoegdheid** | dezelfde twee | de rest van het platform is een toestand en geen bericht, en die staat op `/beheer` |
| **Platformbeheerder**, als gewone gebruiker | type 11 (platformmededelingen en gepland onderhoud), net als iedereen | idem | dat krijgt hij omdat hij gebruiker is, niet omdat hij beheerder is; de reden erbij is `platform-user` en niet `platform-admin` |
| **Platformbeheerder**, voor projecten waar hij zelf lid van is | de projectbeheerdersstandaard hieronder | idem | een beheerder met een eigen project is voor dat project een gewone projectbeheerder |
| **Projectbeheerder** (`admin`, `owner`) | alles van zijn projecten met ernst `actionable` of `outage` | uitrol-mislukkingen, gezondheid, platformingrepen, gegevens, besluiten over zijn aanvragen, ledenwijzigingen | hij is verantwoordelijk voor wat er met het project gebeurt |
| **Projectlid** (`member`, `developer`) | uitrol, gezondheid, verwijderingen, leden, mededelingen, alle met ernst `actionable` of `outage` | alleen mededelingen van het platform, en wat hij zelf startte | hij werkt eraan mee, hij bestuurt het niet |
| **Actor** (bovenop je rol) | wat je zelf startte | mislukkingen van wat je zelf startte | je eigen handeling is altijd van jou, ook als je verder geen rol hebt |

**De regel die dit afdwingt, en hij is één regel in het uitwaaieren:**

> **Een aflevering met `reason = "platform-admin"` wordt niet aangemaakt.**

Dat is de hele correctie. De kolom `reason` draagt `platform-admin` al als aparte waarde naast
`approver`, `actor`, `project-admin` en `project-member`
(`plans/meldingen-oplossingsrichtingen.md`, `notification_deliveries`), dus het onderscheid zit
al in het model en werd alleen niet gebruikt.

**Maar de regel werkt alleen met een rangorde erbij, en dat is nagelopen.** De uniciteitsgrendel
staat op `(event_id, recipient)`, dus een persoon krijgt per gebeurtenis **één** rij met **één**
reden. Iemand kan tegelijk platformbeheerder én projectbeheerder van het getroffen project zijn.
Pakt het uitwaaieren dan de eerste reden die het tegenkomt, en is dat `platform-admin`, dan
verliest die persoon de melding over zijn eigen project. Daarom:

> **De reden is de STERKSTE aanspraak, niet de eerste die gevonden wordt.**
> Volgorde: `actor` > `approver` > `platform-owner` > `project-admin` > `project-member` >
> `platform-user` > `platform-admin`.

Met die volgorde betekent `reason = "platform-admin"` precies één ding: **deze persoon heeft
geen enkele andere aanspraak op deze gebeurtenis.** En dan is "geen postvakrij" het juiste
antwoord, want er is niets waar hij eigenaar van is en niets wat op hem wacht.

**Er komen twee waarden bij**, allebei voorstellen en allebei zonder gevolgen voor het schema
(`reason` is `String(64)` met een vaste waardenlijst in een commentaar,
`plans/meldingen-oplossingsrichtingen.md`, regel 620):

- **`platform-owner`**, als de gebeurtenis over de bevoegdheid van de platformbeheerder zelf
  gaat: hij is platformbeheerder geworden of afgevoerd, of iemand heeft het beheer van een
  project overgenomen. Dat zijn de gevallen waarin hij wél eigenaar is, en zonder deze waarde
  zouden ze onder `platform-admin` vallen en dus verdwijnen.
- **`platform-user`**, voor een bericht dat iedereen krijgt omdat hij gebruiker van het platform
  is: type 11, gepland onderhoud en clusterbrede mededelingen. **Dit is een gat in de
  oorspronkelijke lijst van redenen**, en RC-161 vond het door de regel toe te passen: een
  onderhoudsbericht heeft geen actor, geen goedkeurder en geen project, dus geen van de vijf
  bestaande waarden past erop. Zonder deze waarde zou een platformbeheerder een
  onderhoudsbericht als `platform-admin` binnenkrijgen en het dus juist NIET zien, terwijl hij
  het als iedere andere gebruiker hoort te krijgen.

De volledige rangorde wordt daarmee:

> `actor` > `approver` > `platform-owner` > `project-admin` > `project-member` >
> `platform-user` > `platform-admin`

En de onderdrukkingsregel raakt uitsluitend de laatste.

**En de ernst doet de rest.** De kolom `severity` bestaat (`informational`, `actionable`,
`outage`) en werd nergens gebruikt om te bepalen wat iemand standaard krijgt. Dat wordt nu de
tweede regel:

> **Een gebeurtenis met `severity = "informational"` levert geen postvakrij op.**

Dat is geen derde knop maar de codering van de eerste toets van de grensregel
(`plans/beheer-in-zad-plan-van-aanpak.md`, deel 3): die toets eist dat er een handeling op je
wacht of dat er iets onomkeerbaars met jouw eigendom is gebeurd, en allebei die gevallen zijn
`actionable` of `outage`. Een geslaagde uitrol, een geslaagde backup en een gewekt deployment
zijn `informational` en horen op een pagina, niet in een postvak.

**Eén regel in deel 1 moet daarvoor van ernst veranderen.** "Het platform heeft het geheugen van
een component bijgesteld" staat in `plans/meldingen-inventarisatie.md` paragraaf 4 als "ter
informatie". Dat klopt niet met de tweede regel hierboven, en het klopt ook niet met de
werkelijkheid: de eigenaar kan er wél iets aan doen, want een handmatig gezette waarde wint van
de automatische stemmer (`features/handmatig-gezette-resources.md`). Die regel hoort dus
`actionable` te zijn. Dat is de enige inhoudelijke correctie die RC-161 in de eventcatalogus
aanbrengt.

**Wat er niet overgenomen is, en waarom niet.** De opdracht noemt vier mechanismen; twee zijn
er hierboven overgenomen, één deels, en één niet.

| Mechanisme | Overgenomen? | Reden |
|---|---|---|
| Aan mij gericht versus platformbreed (`reason`) | **ja**, met de rangorde erbij | goedkoopst, en het onderscheid zit al in het model |
| Een drempel op ernst (`severity`) | **ja**, als één vaste regel en niet als een schuifje | het is de codering van de grensregel, geen extra knop |
| Escalatie als niemand kijkt | **deels**: als sortering op ouderdom in het blok "wacht op jou" van `/beheer`, met een markering boven een grens. Een escalerende mail is fase 3 | de sortering kost niets en is er zodra het overzicht er is; een mail kan pas als het e-mailkanaal er is. **Voorwaarde**: de generieke dienstgebruik-goedkeuring schrijft bij het aanvragen een lege history (`opi/services/catalog/approval.py:303`), waar domein en subdomein wél een tijdstip zetten (`opi/connectors/subdomain.py:511` en `:552`). Zonder die ene regel is er geen ouderdom om op te sorteren |
| Per project volgen of dempen (het GitHub-model) | **nee** | met de `reason`-regel hierboven staat er in het postvak van de platformbeheerder alleen nog wat een echte aanspraak heeft. Per-projectdempen lost dan een probleem op dat hij niet meer heeft, en het kost een tabel plus een scherm. Voor gewone projectleden kan het later alsnog waarde hebben; het model sluit het niet uit, want `reason` en een latere abonnementstabel bijten elkaar niet. **En voor het ene geval waarin het wél nodig is, bestaat er al een antwoord**: een platformbeheerder die tijdelijk het beheer van een project overneemt krijgt daarmee `project-admin` als aanspraak, en die vervalt vanzelf met de overname (`plans/beheer-in-zad-plan-van-aanpak.md`, deel 2, "En de handeling erbij", vastgelegd als beslissing 4 in deel 6). Dat is een abonnement dat je niet hoeft te beheren |

### "Waarom kreeg ik dit bericht"

Elke melding draagt hem: de kolom `reason` in `notification_deliveries`
(`actor`, `approver`, `platform-owner`, `project-admin`, `project-member`, `platform-user`;
`platform-admin` staat ook in de lijst maar levert geen postvakrij op, zie "De standaarden per
rol"). In het
postvak staat hij als een regel onder de melding ("Je krijgt dit omdat je beheerder bent van
project X"); in de mail staat hij onderaan, naast de link naar het voorkeurenscherm.

**De reden is de sterkste aanspraak en niet de eerste die gevonden wordt**, in de volgorde
`actor` > `approver` > `platform-owner` > `project-admin` > `project-member` >
`platform-user` > `platform-admin`. Dat is niet alleen een kwestie van een mooiere zin: de
uniciteitsgrendel staat op `(event_id, recipient)`, dus wie twee aanspraken heeft krijgt één rij, en welke reden daarop
belandt bepaalt of hij de melding überhaupt krijgt. Zonder de rangorde raakt een
platformbeheerder die ook projectbeheerder is de meldingen over zijn eigen project kwijt.

**De zin die bij `platform-admin` zou horen bestaat dus niet in het postvak**, en dat is met
opzet. "Je krijgt dit omdat je platformbeheerder bent" is precies de zin waarmee een postvak
ophoudt iets te betekenen: het is geen reden die met dit bericht te maken heeft, het is een
eigenschap van de lezer.

**Dit is de kolom die het model verdient.** Zonder hem is de enige eerlijke tekst "je krijgt dit
omdat een regel ergens vond dat je het moest hebben", en dat is precies de tekst die mensen
alles laat uitzetten.

### Wat niet uitgezet mag kunnen worden

Zo weinig mogelijk, want elke onuitschakelbare melding is er een die mensen leert dat het
scherm niet werkt. Mijn voorstel is **twee gevallen**, en beide alleen voor het postvak (mail
mag altijd uit):

1. **Onomkeerbare ingrepen door het platform.** "Een gemarkeerde resource is definitief
   verwijderd" (deel 1, paragraaf 4). De melding komt achter de daad aan en er is niets meer aan
   te doen. Dat mag niemand hebben gemist, ook niet door een vinkje.
2. **Er is iets veranderd aan wat jij mag.** Je bent uit een project verwijderd, je rol is
   gewijzigd, je bent platformbeheerder geworden of afgevoerd, of iemand heeft het beheer van
   een van jouw projecten overgenomen. Het gaat over jouw eigen toegang. Iemand die dit uitzet,
   weet niet meer waar hij bij mag.

   Dit geval is door RC-161 verbreed van "je projectrol" naar "je bevoegdheid", zodat de twee
   platformbrede gevallen erbij horen zonder dat er een derde onuitschakelbaar type bij komt.
   Dat is dezelfde regel als hierboven, alleen consequent doorgetrokken: het gaat om jouw
   toegang, en op welk niveau die toegang zit is voor de lezer niet het verschil.

**En expliciet WEL uitzetbaar, ook al is het verleidelijk om anders te kiezen**: mislukte
deploys. Een projectlid dat er tien per dag heeft omdat hij aan het uitproberen is, moet ze uit
kunnen zetten, anders zet hij het hele systeem uit. De projectbeheerder houdt ze standaard aan.

---

## De fasering

**De regel: fase 1 past in één PR-serie en heeft op zichzelf waarde.**

### Fase 1: de kern plus één bron, alleen in de UI

**Wat**: het datamodel, de outbox met zijn planner, het postvak, de teller, de API voor lezen
en markeren, en **één** bron: goedkeuringen (aanvraag ingediend, goedgekeurd, afgewezen).

**Waarom goedkeuringen als eerste.** Er zijn er maar drie in de catalogus, ze zijn zeldzaam
(dus geen volumeprobleem in ronde één), de belanghebbende is bijna altijd een
platformbeheerder (dus weinig autorisatiewerk), de aanroeppunten zijn twee functies in
`opi/services/approvals.py`, en de pijn is echt: vandaag ziet niemand een aanvraag tot iemand
`/admin/approvals` opent.

**Waarde op zichzelf**: een platformbeheerder ziet aan de teller in de kop dat er een aanvraag
op hem wacht. Dat is af, ook als er nooit een tweede fase komt.

**Wat RC-161 aan deze fase verandert.** De keuze voor goedkeuringen als eerste bron blijft
staan, en de grensregel bevestigt hem: type 6 is de schoonste doorgang van de eerste toets in de
hele catalogus (`plans/beheer-in-zad-plan-van-aanpak.md`, deel 3). Er veranderen drie dingen:

1. **Er komt een fase 0 vóór deze fase**, en die is klein: het beheerdersoverzicht `/beheer` met
   de blokken "wacht op jou" en "niet gezond". Geen tabel, geen migratie, geen planner: allebei
   die blokken bevragen toestand die er al is. De reden voor die volgorde is dat fase 1 de
   standaardentabel in code vastlegt en fase 2 hem erft; het overzicht eerst bouwen maakt "naar
   het overzicht" een bestaande bestemming in plaats van een belofte. **Wat het kost**: de
   meldingen schuiven op met de bouwtijd van fase 0. Zie beslissing 8 in
   `plans/beheer-in-zad-plan-van-aanpak.md`, waar ook staat wat het alternatief is als de
   opdrachtgever die vertraging niet wil.
2. **Fase 1 levert de gecorrigeerde standaardentabel**, niet de oude. Concreet: de twee regels
   uit "De standaarden per rol" hierboven (geen aflevering bij `reason = "platform-admin"`, geen
   aflevering bij `severity = "informational"`) horen bij het uitwaaieren dat in deze fase
   gebouwd wordt, en niet in een latere fase. Ze zijn later toevoegen betekent een uitgerolde
   standaard corrigeren, en dat is duurder dan hem meteen goed leggen.
3. **Fase 1 repareert de ontbrekende aanvraagdatum.** De generieke dienstgebruik-goedkeuring
   schrijft bij het aanvragen een lege history (`opi/services/catalog/approval.py:303`), waar
   domein en subdomein een tijdstip zetten (`opi/connectors/subdomain.py:511` en `:552`).
   Zonder die ene regel is bij een `send-email`-aanvraag niet vast te stellen hoe lang hij
   ligt, en dan kan het blok "wacht op jou" niet op ouderdom sorteren en kan de escalatievraag
   later niet beantwoord worden.

**Wat er NIET aan deze fase verandert**: de bestandenlijst hieronder, het datamodel, de
outboxplanner, de keuze voor een eigen planner in de lifespan en de bewaartermijnen.

**Bestanden die geraakt worden:**

| Bestand | Wat |
|---|---|
| `opi/services/persistence/notifications.py` | nieuw: de vier ORM-modellen |
| `opi/services/persistence/__init__.py` | de vier modellen erbij importeren |
| `opi/migrations/versions/005_add_notifications.py` | nieuw |
| `opi/services/notifications.py` | nieuw: aanleggen, uitwaaieren, dedup, lezen, markeren |
| `opi/core/notification_scheduler.py` | nieuw: de outboxplanner, in de vorm van de zes bestaande |
| `opi/server.py` | de planner starten en stoppen in de lifespan, en de twee routers registreren |
| `opi/core/config.py` | de instellingen (aan/uit, interval, bewaartermijnen) |
| `opi/services/approvals.py` | de aanroepen bij `ensure_approval_requests` en `apply_approval_verdicts` |
| `opi/api/v2/notifications_router.py` | nieuw: de zes endpoints |
| `opi/api/v2/models.py` | de antwoordmodellen |
| `opi/web/router_notifications.py` | nieuw: het postvak en het tellerfragment |
| `opi/templates_lotc/base_lotc.html.j2` | de teller in de hulpbalk |
| `opi/templates_lotc/notifications/*.html.j2` | nieuw: de pagina en het fragment |
| `opi/web/menu.py` | het postvak in het menu |
| `opi/web/lotc_fixtures.py` | de proefopstelling, met zichtbaar verzonnen waarden |
| `tests/test_notifications.py` | nieuw |
| `tests/e2e/test_notifications.py` | nieuw |
| `features/meldingen.md` | nieuw |

**Wat er in fase 1 bewust NIET gebeurt:**

- geen e-mail, geen Mattermost (alleen het postvak);
- geen voorkeurenscherm (de standaarden per rol staan vast in de code; de tabel
  `notification_preferences` wordt wel aangelegd zodat fase 3 geen migratie is);
- geen draadgroepering in de UI (de kolom `thread_key` wordt wel gevuld);
- geen samenvattingen;
- geen dedupvenster met een teller (wel de `dedup_key`, want die is achteraf niet te vullen).

De regel achter dat lijstje: **kolommen die je later niet meer kunt vullen, worden nu gevuld;
gedrag dat je later kunt toevoegen, wordt later toegevoegd.**

### Fase 2: de bronnen erbij

**Wat**: taken (de vijf groepen), gezondheid, platformingrepen, backups, leden en toegang,
werkomgevingen. Plus het dedupvenster met de teller, want vanaf hier is het volume echt.

**Bestanden**: `opi/core/task_worker.py` (één plek voor 23 taaksoorten),
`opi/services/oom_watcher.py`, `opi/services/resource_tuning_service.py`,
`opi/services/catalog/sleep_mode/scheduler.py`, `opi/jobs/reconciliation.py`,
`opi/services/runs_service.py`, en de opslagweg van het projectbestand voor de vergelijking
oud-nieuw.

**Niet doen**: de "bestaat nog niet"-regels uit de inventarisatie waarvoor eerst iets
waargenomen moet worden (gezond-naar-ongezond-overgangen, "al N dagen geen backup"). Die
vragen toestandsgeheugen en dat is een eigen stuk.

### Fase 3: de voorkeuren en het e-mailkanaal

**Wat**: het voorkeurenscherm, de standaardprofielen per rol, de SMTP-connector, de
samenvatting, de afmeldlink.

**Bestanden**: `opi/connectors/mail_sender.py` (nieuw, of erbij in `opi/connectors/mail.py`),
`opi/web/router.py:2608` (daar zit `/account` vandaag; het voorkeurenscherm hoort ernaast),
`opi/templates_lotc/account/meldingen.html.j2`,
`opi/api/v2/notifications_router.py` (de twee voorkeurendpoints).

**Voorwaarde die buiten dit traject ligt**: de relay moet op productie aanstaan
(`plans/mail-vervolgpunten.md` punt 5), en er moet een antwoord zijn op de vraag wat er gebeurt
met een ontvanger buiten `rijksoverheid.nl` (punt 8).

### Fase 4: Mattermost en het inkomende endpoint

**Wat**: de bot, de koppeling met verificatiecode, en het inkomende endpoint waarop een systeem
van buiten een gebeurtenis kan aanbieden.

**Voorwaarde**: het antwoord op "welke Mattermost en waar staat hij".

### Wat we bewust NIET doen, in het hele traject

| Niet | Waarom |
|---|---|
| Een uitgaand abonnement of webhook naar derden | geen afnemer; de Abonneren-standaard is nog een werkversie; het CloudEvents-record houdt het open |
| De logbewaker vervangen | ander publiek (ops), andere bron (Loki), ander kanaal (ntfy); zie deel 1 paragraaf 9 |
| Een audittabel bouwen | de gebeurtenistabel is zo gebouwd dat hij het kan worden; het zelf tot doel maken is een eigen opdracht |
| Meldingen over applicatiegedrag van de klant | dat is van het project, niet van het platform |
| Metrieken en drempelwaarden | Prometheus en Grafana doen dat, met hun eigen alarmering |
| Meldingen op basis van de beveiligingsscan, image-veroudering of certificaatverval | die gebeurtenissen bestaan niet in OPI; fase 4 maakt ze mogelijk via het inkomende endpoint |
| De websocket-router uitbreiden voor meldingen | per werker geboekhoud, en een permanente verbinding per tabblad is een andere orde dan een logvenster |
| Een `withdrawn`-status voor aanvragen | dat is een uitbreiding van de goedkeuringsmachine, niet van meldingen |
| Meldingen per taaksoort of per dienst instelbaar maken | 23 keer 23 knoppen; de verfijning zit in de melding, niet in het scherm |

---

## De openstaande beslissingen

Elk punt is met ja of nee te beantwoorden. De aanbeveling staat erbij.

**1. Richting C (postvak per persoon), met de gebeurtenistabel van B eronder en de
CloudEvents-vorm van D.**
*Aanbeveling: ja.* A geeft niet wat de wens vraagt; B alleen kan de melding "je bent uit dit
project verwijderd" principieel niet bezorgen en laat de geschiedenis meebewegen met het
lidmaatschap. Zie deel 2.

**2. Twaalf meldingstypen, en per type per kanaal een knop (in plaats van één knop per type).**
*Aanbeveling: ja voor allebei*, met werkbare standaarden per rol zodat niemand het scherm hoeft
te openen. Het getal twaalf is bespreekbaar; de regel eronder (één type per beslissing die een
redelijk mens anders zou nemen) is dat minder.

**3. Fase 1 is goedkeuringen, alleen in het postvak.**
*Aanbeveling: ja.* Weinig volume, weinig autorisatiewerk, twee aanroeppunten, en de pijn is
echt. Het alternatief (beginnen met mislukte taken) is waardevoller voor de gebruiker maar
raakt meteen het volume, de dedup en de bewaartermijn.
*Bijgewerkt door RC-161*: de bronkeuze blijft, maar er komt een kleine fase 0 vóór (het
beheerdersoverzicht) en fase 1 levert de gecorrigeerde standaardentabel. Zie "Fase 1" hierboven
en beslissing 8 in `plans/beheer-in-zad-plan-van-aanpak.md`.

**4. De gebeurtenissen ontstaan met losse aanroepen op de plek waar ze gebeuren, plus een
vergelijking oud-nieuw op de opslagweg van het projectbestand.**
*Aanbeveling: ja.* Het bestaande hakensysteem (`ActionEvent`) heeft twee leden en is een
uitbreidingspunt in de UITROLcyclus; bijna geen enkele gebeurtenis uit de inventarisatie past
daarop, en het commit-contract van die familie botst met de outbox. Uitbreiden kan, maar dat is
een verbouwing van dat systeem en geen gebruik ervan.

**5. Een eigen outboxplanner in de lifespan van `server.py`, niet de bestaande takenwerker.**
*Aanbeveling: ja.* De takenwerker verwerkt één zware taak tegelijk; een melding zou achter een
uitrol in de wachtrij komen. Een eigen planner is de vorm die er al zes keer staat.

**6. De bewaartermijnen: gebeurtenissen 1 jaar, gelezen meldingen 90 dagen, ongelezen
meldingen zolang de gebeurtenis bestaat, afleveringen 30 dagen.**
*Aanbeveling: ja.* Merk op dat dit veel langer is dan wat er nu voor taken geldt (één uur), en
dat is de bedoeling: die ene uur is precies het probleem dat dit oplost.

**7. Het platformaccount op de relay krijgt een weergavenaam ("ZAD"), en geen eigen
plusdeel-adres.**
*Aanbeveling: ja.* De machinerie bestaat (`set_sender_name`), het adres blijft het kale
`noreply-rijksapp@rijksoverheid.nl`, en de conventie "plusdeel = project" blijft intact.

**8. E-mail is fase 3 en geen fase 1.**
*Aanbeveling: ja.* Het account, het netwerkpad en de relay-machinerie liggen er; wat ontbreekt
is de SMTP-client. Maar het kanaal is pas eerlijk als de relay op productie aanstaat en als
duidelijk is wat er gebeurt met een ontvanger buiten `rijksoverheid.nl`.

**9. Mattermost: een bot met privéberichten, met zelfkoppeling via een verificatiecode. Een
kanaalwebhook is iets anders en komt later, als PROJECTinstelling.**
*Aanbeveling: ja*, met dit voorbehoud: eerst moet vaststaan welke Mattermost het is en of hij
op het internet staat. Staat hij achter het Rijksnetwerk, dan kan OPI er niet bij en is er een
tussenstation in een eigen namespace nodig, precies zoals bij de mailrelay.

**10. Een INKOMEND endpoint voor gebeurtenissen van buiten (fase 4), en geen uitgaand
abonnement.**
*Aanbeveling: ja.* Het inkomende endpoint maakt drie "bestaat nog niet"-regels uit de
inventarisatie haalbaar voor de prijs van één route. Het uitgaande abonnement heeft geen
afnemer en de standaard ervoor is nog niet vastgesteld.

**11. De `source`-URN voor CloudEvents vraagt een OIN, en dat is een organisatievraag.**
*Aanbeveling: nu een vaste vorm met een plaatshouder vastleggen, zodat het later één keer
invullen is.* Dit is het enige punt in deze lijst waar de bouwer niets kan beslissen.

**12. Twee dingen mogen niet uitgezet worden in het postvak: onomkeerbare ingrepen door het
platform, en wijzigingen aan je eigen bevoegdheid.**
*Aanbeveling: ja, en niet meer dan die twee.* Elke onuitschakelbare melding erbij leert mensen
dat het voorkeurenscherm niet werkt.
*Bijgewerkt door RC-161*: het tweede geval heette "je eigen toegang" en betrof alleen je rol in
een project. Het is verbreed naar je bevoegdheid, zodat "je bent platformbeheerder geworden of
afgevoerd" en "iemand nam het beheer van jouw project over" erbij horen zonder dat er een derde
onuitschakelbaar type bij komt.

**13. Een aflevering met `reason = "platform-admin"` wordt niet aangemaakt, en de reden is de
sterkste aanspraak in plaats van de eerste die gevonden wordt.**
*Aanbeveling: ja.* Dit is de kern van de correctie op "platformbeheerder: alles". Zonder de
rangorde erbij is de regel fout, want de uniciteitsgrendel op `(event_id, recipient)` laat maar
één reden toe en dan verliest een platformbeheerder die ook projectbeheerder is de meldingen
over zijn eigen project.

**14. Een gebeurtenis met `severity = "informational"` levert geen postvakrij op.**
*Aanbeveling: ja.* `severity` bestond al en werd nergens gebruikt om te bepalen wat iemand
krijgt. Dit is geen extra knop maar de codering van de eerste toets van de grensregel. Let op de
consequentie: de regel "Het platform heeft het geheugen van een component bijgesteld" in deel 1
paragraaf 4 moet dan van "ter informatie" naar "actie nodig", anders komt hij bij niemand aan.

**15. Per project volgen of dempen (het GitHub-model) komt er niet.**
*Aanbeveling: ja, niet doen.* Met beslissing 13 staat er in het postvak van de platformbeheerder
alleen nog wat een echte aanspraak heeft, en dan lost per-projectdempen een probleem op dat hij
niet meer heeft. Het model sluit het niet uit voor later.

---

## Wijzigingslijst RC-161

Op 28 augustus 2026 is dit document bijgewerkt naar aanleiding van RC-161 ("Het beheerdeel van
ZAD: rollen, overzicht, en wat een beheerder moet weten"). De aanleiding: de standaard
"platformbeheerder: alles, inclusief type 12" is niet wat de opdrachtgever wil, en het gat
eronder is dat er geen beheerdersoverzicht is om de rest naartoe te sturen. De onderbouwing en
de metingen staan in `plans/beheer-in-zad-inventarisatie.md` en
`plans/beheer-in-zad-plan-van-aanpak.md`.

| Waar | Wat | Waarom |
|---|---|---|
| Kop van het document | een verwijzing naar deze lijst | twee documenten die elkaar tegenspreken zijn erger dan één dat is bijgewerkt |
| "De verversingsweg", kop | "en niet de websocket" werd "en niet de websocket of SSE" | de kop beloofde een afweging die de middenweg oversloeg |
| "De verversingsweg", nieuwe subparagraaf | de weging van server-sent events, met of zonder `LISTEN/NOTIFY`, per plek in plaats van in het algemeen, plus de metingen eronder | de uitkomst blijft peilen, maar hij is nu een beslissing en geen overgeslagen alternatief. Gemeten: OPI draait als één proces, dus het per-werker-bezwaar tegen de websocket bijt vandaag niet; de CSP staat een `EventSource` toe zonder wijziging; en de router knipt elke stroom via `zad.rijksapp.nl` na 300 seconden af |
| "Het scherm" | een regel boven de voorkeurentabel voor de platformbeheerder | hij is voor zijn eigen projecten een gewone projectbeheerder; een tweede tabel zou suggereren dat hij twee verzamelingen voorkeuren heeft |
| "De standaarden per rol", tabel | de ene regel voor de platformbeheerder is **drie** regels geworden (in die hoedanigheid, als gewone gebruiker, en als lid van zijn eigen projecten), en zijn postvak ging van "alles, inclusief type 12" naar type 6 plus wijzigingen aan zijn eigen bevoegdheid; bij projectbeheerder en projectlid is `severity` toegevoegd | de opdrachtgever hoeft niet voor alle projecten alles te zien, en de grensregel wijst van de twaalf typen er één naar zijn beheerderspostvak (type 6), plus de twee uitzonderingen in type 12 die over zijn eigen bevoegdheid gaan. De derde regel is nodig omdat type 11 hem als **gebruiker** bereikt en niet als beheerder: die aflevering draagt `platform-user` en zou onder `platform-admin` verdwijnen |
| "De standaarden per rol", tabel | de E-mailkolom van de platformbeheerder ging van "aanvragen, storingen, onomkeerbare ingrepen" naar "dezelfde twee" (en "idem" op de twee regels eronder) | e-mail ruimer laten dan het postvak zou de correctie via de achterdeur ongedaan maken: dan is hij de firehose kwijt op het scherm en houdt hij hem in zijn mailbox |
| "De standaarden per rol", nieuw | de regel "een aflevering met `reason = platform-admin` wordt niet aangemaakt", mét de rangorde van redenen eronder | het onderscheid zat al in de kolom `reason` en werd niet gebruikt. De rangorde is nodig, want zonder hem verliest een platformbeheerder die ook projectbeheerder is de meldingen over zijn eigen project: de uniciteitsgrendel op `(event_id, recipient)` laat maar één reden toe |
| "De standaarden per rol", nieuw | twee redenen erbij: `platform-owner` en `platform-user` (allebei voorstel) | zonder `platform-owner` zouden "je bent platformbeheerder geworden" en "iemand nam het beheer van jouw project over" onder `platform-admin` vallen en dus verdwijnen, terwijl hij daar juist eigenaar is. `platform-user` dekt een **gat in de oorspronkelijke lijst**: een clusterbrede mededeling heeft geen actor, geen goedkeurder en geen project, dus geen van de vijf bestaande waarden paste erop |
| "De standaarden per rol", nieuw | de regel "`severity = informational` levert geen postvakrij op" | `severity` bestond en werd nergens gebruikt om te bepalen wat iemand krijgt; dit is de codering van de eerste toets van de grensregel en geen extra knop |
| "De standaarden per rol", nieuw | een tabel met wat er van de vier voorgestelde mechanismen wél en niet is overgenomen | per-project volgen of dempen is bewust niet overgenomen; dat hoort erbij te staan met de reden |
| "Waarom kreeg ik dit bericht" | de rangorde van redenen, en de vaststelling dat de zin bij `platform-admin` niet bestaat | "je krijgt dit omdat je platformbeheerder bent" is geen reden die met het bericht te maken heeft, maar een eigenschap van de lezer |
| "Wat niet uitgezet mag kunnen worden" | geval 2 is verbreed van "je projectrol" naar "je bevoegdheid" | zo horen de twee platformbrede gevallen erbij zonder dat er een derde onuitschakelbaar type bij komt, en het document blijft bij zijn eigen regel dat het er zo weinig mogelijk moeten zijn |
| "Fase 1" | drie wijzigingen: er komt een kleine fase 0 vóór, fase 1 levert de gecorrigeerde standaardentabel, en fase 1 repareert de ontbrekende aanvraagdatum | de bronkeuze (goedkeuringen) blijft; wat verandert is de volgorde en de standaard die erin gebakken wordt |
| "De openstaande beslissingen", punt 3 | een regel erbij over fase 0 en de standaardentabel | anders spreekt de beslissingenlijst de fasering erboven tegen |
| "De openstaande beslissingen", punt 12 | "je eigen toegang" werd "je eigen bevoegdheid" | dezelfde verbreding als in de lijst hierboven |
| Kanaal 2, "Wat er al ligt" | het anker naar TaskResponse kreeg zijn volledige pad: `opi/api/task_models.py:416` | een kaal bestandsnaam-anker is niet na te lopen; de rest van het document schrijft het volledige pad |
| "De openstaande beslissingen" | drie punten erbij (13, 14, 15) | de `reason`-regel met zijn rangorde, de ernstregel, en het besluit om per-projectdempen niet te bouwen. Elk is met ja of nee te beantwoorden en heeft gevolgen voor de bouw, dus ze horen in deze lijst en niet alleen in de proza |

### Wat er in de andere twee documenten van RC-148 verandert

**`plans/meldingen-oplossingsrichtingen.md`: niets in dit document zelf.** Het datamodel blijft
staan zoals het er ligt. De twee dingen die RC-161 toevoegt passen erin zonder wijziging:
`platform-owner` en `platform-user` zijn extra waarden in een kolom die al een vaste
waardenlijst heeft (`reason`, `String(64)`, `plans/meldingen-oplossingsrichtingen.md` regel
620), en de rangorde van redenen is gedrag in het uitwaaieren en geen schema. Wat er wél in dat
document mag: het commentaar achter die kolom noemt vijf waarden en er worden er zeven, dus dat
commentaar loopt achter zodra dit gebouwd wordt. Ook `severity` wordt gebruikt zoals hij
bedoeld was, met de drie waarden die er al staan. **Er is dus geen migratie en geen kolom
bij.**

**`plans/meldingen-inventarisatie.md`: één inhoudelijke correctie, nog niet doorgevoerd.** In
paragraaf 4 staat "Het platform heeft het geheugen van een component bijgesteld" met ernst "ter
informatie". Dat hoort `actie nodig` te zijn: de eigenaar kan er wel degelijk iets aan doen,
want een handmatig gezette waarde wint van de automatische stemmer
(`features/handmatig-gezette-resources.md`). Met de nieuwe ernstregel zou de melding anders bij
niemand aankomen, terwijl dit juist het type is waarvan de opdrachtgever zegt dat de eigenaar
het achteraf moet weten. Deze correctie staat hier gemeld en is bewust niet in dat document
doorgevoerd, omdat de inventarisatie een meting is en de reden voor de wijziging in dit document
thuishoort.

**En één bevinding die geen correctie is maar wel gemeld moet worden.** De inventarisatie zegt
in paragraaf 7 over "Iemand is platformbeheerder geworden of afgevoerd": "**bestaat nog niet**:
de allowlist komt uit de configuratie". Dat klopt en het blijft kloppen, maar het is met dit
voorstel geen permanente toestand meer: beslissing 9 in
`plans/beheer-in-zad-plan-van-aanpak.md` verhuist die lijst naar de database met een handeling
erachter, en daarmee wordt die regel wél een gebeurtenis. Het is dus geen fout in de
inventarisatie maar een regel die door dit voorstel van kolom verandert.

