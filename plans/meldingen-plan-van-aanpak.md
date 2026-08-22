# Meldingen in ZAD: de kanalen en het plan van aanpak

**Geschreven op**: 22 augustus 2026. Dit is deel 3 van drie. Deel 1 (de eventcatalogus) staat
in `plans/meldingen-inventarisatie.md`, deel 2 (de oplossingsrichtingen en het datamodel) in
`plans/meldingen-oplossingsrichtingen.md`.

Dit deel gaat over de vier kanalen (wat is er nodig, wat ligt er al, wat blokkeert), over het
voorkeurenscherm, en daarna over de fasering en de beslissingen die de opdrachtgever moet
nemen.

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

### De verversingsweg: peilen vanuit de browser, en niet de websocket

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
  (`TaskResponse[TResult]`, `task_models.py:416`).
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

### De standaarden per rol

Uit de tabel in deel 1, samengevat:

| Rol | Postvak | E-mail | Redenering |
|---|---|---|---|
| **Platformbeheerder** | alles, inclusief type 12 (beheer) | aanvragen, storingen, onomkeerbare ingrepen | hij is de eerstelijns; de rest ziet hij als hij kijkt |
| **Projectbeheerder** (`admin`, `owner`) | alles van zijn projecten | uitrol-mislukkingen, gezondheid, platformingrepen, gegevens, besluiten over zijn aanvragen, ledenwijzigingen | hij is verantwoordelijk voor wat er met het project gebeurt |
| **Projectlid** (`member`, `developer`) | uitrol, gezondheid, verwijderingen, leden, mededelingen | alleen mededelingen van het platform, en wat hij zelf startte | hij werkt eraan mee, hij bestuurt het niet |
| **Actor** (bovenop je rol) | wat je zelf startte | mislukkingen van wat je zelf startte | je eigen handeling is altijd van jou, ook als je verder geen rol hebt |

### "Waarom kreeg ik dit bericht"

Elke melding draagt hem: de kolom `reason` in `notification_deliveries`
(`project-admin`, `project-member`, `actor`, `approver`, `platform-admin`). In het postvak
staat hij als een regel onder de melding ("Je krijgt dit omdat je beheerder bent van
project X"); in de mail staat hij onderaan, naast de link naar het voorkeurenscherm.

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
2. **Je bent uit een project verwijderd, of je rol is gewijzigd.** Het gaat over jouw eigen
   toegang. Iemand die dit uitzet, weet niet meer waar hij bij mag.

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
platform, en wijzigingen aan je eigen toegang.**
*Aanbeveling: ja, en niet meer dan die twee.* Elke onuitschakelbare melding erbij leert mensen
dat het voorkeurenscherm niet werkt.
