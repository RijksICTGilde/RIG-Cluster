# Gebeurtenissen in ZAD: de kanalen en het plan van aanpak

## Status en meetbasis

Dit is deel 3 van drie. Deel 1 (`features/futures/gebeurtenissen-inventarisatie.md`) is het feitenmateriaal, deel 2 (`features/futures/gebeurtenissen-vastleggen-en-melden.md`) weegt de richtingen af en beslecht de tegenspraken, en dit document kiest een weg en zet hem in fasen. Wie het oneens is met de keuze hieronder kan deel 1 en 2 los gebruiken.

**De meetbasis is commit `83ac4b9b` van 21 augustus 2026**, dezelfde als in deel 1 en 2, en niet `main` zoals dat er toen bij stond. Zie deel 1 voor waarom dat verschil ertoe deed en waarom het inmiddels is opgelost. **Alle namen van tabellen, kolommen, gebeurtenissoorten, endpoints en routes hieronder zijn een VOORSTEL**, tenzij er een codeanker bij staat.

**Herkomst.** Dit document is de samenvoeging van het inmiddels verwijderde `plans/meldingen-plan-van-aanpak.md` (22 augustus 2026) en de eerdere versie van dit bestand (21 augustus 2026), gedaan in RC-163 op 28 augustus 2026. De eerste leverde de vier kanalen elk uitgewerkt, het voorkeurenscherm en de openstaande beslissingen; de tweede leverde de kleinste eerste stap, de volgorde van de kanalen met de onderbouwing, en de webhook en Alertmanager als eigen kanalen. Waar de twee elkaar tegenspraken staat de beslechting in de tekst. Er zijn hier twee zulke plekken: de volgorde van de kanalen (hieronder), en welke bron de eerste schrijfweg wordt (in de fasering).

---

## De aanbeveling in een alinea

Leg gebeurtenissen vast in een eigen tabel in rig-db, als vijfde Alembic-migratie naast `async_tasks`, `runs`, `users` en `marked_for_deletion`, met een gesloten enum van soorten waarvan de waarden meteen in reverse-DNS-notatie staan zodat een CloudEvents-projectie later alleen nog `source` hoeft in te vullen. Toon ze eerst in de portal als tijdlijn per project en per deployment, achter de autorisatie die er al is. Zet daar het postvak per persoon bovenop zodra de gebeurtenissen die je vastlegt de goede blijken. Bouw pas daarna push-kanalen, en dan in de volgorde webhook, Alertmanager, mail, Mattermost, omdat dat de volgorde is van "in ons beheer" naar "afhankelijk van een keten die aantoonbaar nog niet af is".

---

## De kleinste eerste stap

**Als er maar een ding wordt gebouwd: de tabel plus precies een schrijfweg, namelijk de resource-tuner, en een tijdlijn op de deploymentpagina die hem toont.**

Waarom deze en geen andere.

De resource-tuner is de enige bron die vandaag al een compleet gevormde gebeurtenis produceert. `$defs/resource-history-entry` in `opi/schemas/project_v2.json` heeft al een `timestamp`, een `source` uit een gesloten lijst (`auto-tune`, `oom-watcher`, `manual`), een `deployment` en een `reason` in proza. Er ontbreekt alleen een actor, en bij een scheduler is de actor de scheduler; in het model van deel 2, punt 7 betekent dat `actor_kind = scheduler` met `actor` op `NULL`, want die kolom draagt een e-mailadres en een scheduler heeft er geen. Er hoeft dus niets te worden bedacht over wat er in de gebeurtenis staat; het staat er al, alleen op de verkeerde plek.

Het is bovendien de gebeurtenis waar het startpunt van deze opdracht mee opent: "een deployment die om drie uur 's nachts vanzelf meer geheugen kreeg". Die is vandaag alleen te zien door het projectbestand open te slaan, en dat doet een gebruiker niet.

En het is de goedkoopste manier om te ontdekken of de kolomkeuze klopt, want deze ene soort raakt alle niveaus (project, deployment, component), heeft een niet-menselijke actor, en heeft zowel gestructureerde gegevens als een mensleesbare samenvatting.

**Verifieerbare uitkomst:** een nachtelijke tuningronde op de sandbox schrijft een rij in de gebeurtenissentabel, en die rij is als regel zichtbaar op de deploymentpagina voor een projectlid en niet zichtbaar voor iemand die geen lid is. Een herstart van OPI daartussen verandert daar niets aan.

**Wat er expliciet niet in zit:** geen tweede bron, geen postvak, geen uitwaaiering, geen melding, geen abonnement, geen export, geen retentielus. Die komen in de fasen erna, en de tabel is zonder die dingen al bruikbaar.

---

## Beslecht: de volgorde waarin de kanalen worden gebouwd

**De tegenspraak.** Het ene bronstuk zet de kanalen in de volgorde postvak in de UI, dan de bronnen erbij, dan de voorkeuren en e-mail, dan Mattermost en een inkomend endpoint; een webhook staat daar op de niet-doen-lijst, met drie redenen: er is geen afnemer, de Abonneren-standaard van Logius is nog een werkversie, en elke uitgaande verbinding is een netwerkbeleidsgesprek. Het andere zet de volgorde op tijdlijn in de portal, dan webhook, dan Alertmanager, dan mail, met als onderbouwing dat dat de volgorde is van "in ons beheer" naar "afhankelijk van een keten die aantoonbaar nog niet af is", en dat een meldkanaal dat stil faalt erger is dan geen meldkanaal.

De twee zijn het over de kop en over de staart eens: de portal eerst, en mail laat, en om precies dezelfde gemeten reden. Ze verschillen over de webhook.

**Beslecht: de webhook is het eerste push-kanaal, omdat de twee bronstukken het over twee verschillende dingen hebben.** De drie redenen om hem af te wijzen gaan over een uitgaand ABONNEMENT naar DERDEN: een andere overheidspartij die zich abonneert op onze gebeurtenissen, in de zin van de Abonneren-standaard. Dat blijft buiten scope, en die drie redenen blijven daarvoor gelden. Een **webhook per project naar de eigen tooling van dat project** is iets anders: de ontvanger is dezelfde partij die vandaag al de `X-API-Key` van dat project houdt, er is dus wel een afnemer, er is geen standaard nodig om hem te bedienen, en het netwerkbeleid is al gemeten (de namespace draagt `egress.projectcalico.org/egressGatewayPolicy: "internet"` en het beleid laat uitgaand 443 naar elke bestemming toe, deel 1 paragraaf 9). Van de drie redenen blijft er als BLOKKADE geen over.

**De derde reden verandert daarmee van blokkade in eis, en dat is geen woordspel.** "Elke uitgaande verbinding vanaf dit cluster is een netwerkbeleidsgesprek" was in het bronstuk een KOSTENargument, en als kostenargument klopt het niet meer: de regel staat er al. Maar precies dezelfde meting die de kosten wegneemt is de reden om hier voorzichtig te zijn. `to: []` op TCP/443 betekent ELKE bestemming, ook alles wat alleen vanuit het cluster bereikbaar is, en de bezorger draait met de netwerkpositie van OPI en niet met die van het project. Wat het netwerkbeleid niet meer afdwingt moet het kanaal zelf afdwingen: dat is ontwerpregel 4 bij Kanaal 5, en zonder die regel is een abonnement geen meldkanaal maar een verzoekvervalsing vanaf de serverkant met een herhaalmechanisme eraan.

En de reden om hem juist vroeg te doen is sterk: hij bedient publiek C, dat vandaag structureel het slechtst bediend is (deel 1), hij heeft geen persoonsgegevens nodig in het kanaal zelf, hij is per project af te schermen, en hij hangt niet af van een keten buiten ons beheer.

**De volgorde wordt daarmee:** de tijdlijn in de portal, dan het postvak per persoon, dan de webhook per project, dan Alertmanager, dan mail, dan Mattermost. Alertmanager voor mail omdat hij binnen ons beheer valt en mail niet. Mattermost achteraan omdat er nog een blokkerende vraag open staat die niemand in de code kan beantwoorden.

**Wat de verliezende redenering aandroeg en wat overeind blijft:** het onderscheid zelf. Zonder die drie redenen zou een uitgaand abonnement naar derden er stilzwijgend bij zijn gefietst, en daar geldt nog steeds dat er geen afnemer is en dat de standaard niet vaststaat. Dat staat nu expliciet op de niet-doen-lijst, en het inkomende endpoint dat het andere bronstuk voorstelt blijft precies daar staan waar het thuishoort: als goedkope tegenhanger, niet als opmaat naar een uitgaand abonnement.

---

De nummering hieronder is de volgorde waarin de kanalen beschreven zijn, niet de volgorde waarin ze gebouwd worden. Die staat in de fasering.

## Kanaal 1: de UI, de tijdlijn en het postvak

### Wat er nodig is

Vier dingen, en niet meer:

1. **Een tijdlijn per project en per deployment.** Een blok op de bestaande pagina met de gebeurtenissen van dat onderwerp, nieuwste eerst, achter `is_user_authorized_for_project`. Dit is het goedkoopste stuk van het hele traject en het is het eerste dat gebouwd wordt: het vraagt geen uitwaaiering, geen abonnement, geen afmeldpad, en het kan niet mislukken bij de bezorging. Het is bovendien de enige manier om te controleren of de gebeurtenissen die je vastlegt de goede zijn, voordat je ze naar iemands postvak stuurt. Een meldkanaal bouwen op gebeurtenissen die je nog niet hebt bekeken is de snelste weg naar een kanaal dat wordt uitgezet.
2. **Een teller in de kop.** Het aantal ongelezen meldingen, klikbaar. In de hulpbalk rechtsboven, naast het accountmenu, want dat is waar de gebruiker hem verwacht en het is de enige plek in de schil die op elke pagina hetzelfde is (`opi/templates_lotc/base_lotc.html.j2`, het blok rond `is_ingelogd`).
3. **Een postvak.** Een pagina met de meldingen, nieuwste eerst, gegroepeerd op draad (`thread_key` uit het datamodel). Per regel: wat er gebeurde, waar het over ging, hoe lang geleden, en waarom je het ziet. Filters op gelezen/ongelezen en op type. Knoppen om een melding of alles als gelezen te markeren.
4. **Een weg terug naar het onderwerp.** Elke melding moet ergens heen wijzen: de projectdetailpagina, de deploymentkaart, `/admin/approvals`. Dat is geen extra kolom in de tabel maar een functie die uit `type` plus `project` plus `deployment` een pad maakt, want de bestemming volgt uit het type en hoort niet per melding opgeslagen te worden (dan is een verhuisde pagina een migratie).

Let op het onderscheid uit deel 2, punt 4: de tijdlijn volgt het HEDEN (wie geen lid meer is, ziet hem niet meer) en het postvak volgt het VERLEDEN (wie geen lid meer is, houdt wat er voor hem is neergelegd). Dat is met opzet, en het hoort ook op het scherm zichtbaar te zijn: de tijdlijn heet "wat er met dit project is gebeurd" en het postvak heet "jouw meldingen".

### Wat er al ligt

- **De schil en de componenten.** LOTC met het NLDD-thema, `base_lotc.html.j2`, `opi/web/navigation_lotc.py` voor de indeling. In gebruik in `opi/templates_lotc/` zijn onder meer `c-table`, `c-card`, `c-alert`, `c-icon` en `c-tag`. Wat we hier nodig hebben staat al in de bibliotheek zelf (de catalogus, niet het gebruik: `lord_of_the_components/templates/components/` en `registry.json`), en dat is meer dan uit het gebruik blijkt. Drie dingen die de bouwer moet weten voor hij begint:
  - **De naam `c-notification` is bezet, en niet door ons.** `notification.html.j2` en `notification-item.html.j2` bestaan al in de bibliotheek (een `<ul>` met per regel icoon, titel, optionele link en een metaregel) en worden gebruikt in `opi/templates_lotc/bg/feedback.html.j2:84` als **vluchtige bevestiging** na een actie ("Project opgeslagen", "Uitrollen wacht"). Dat is geen postvak: het is de terugkoppeling die verschijnt en weer weggaat. Er mag dus geen tweede `c-notification` gebouwd worden. Kies bij het bouwen bewust: of je hergebruikt `c-notification-item` letterlijk voor een postvakregel (de vorm past), of je geeft de nieuwe component een naam die niet botst (VOORSTEL: `c-inbox-item`). Wat niet mag is de bestaande naam overnemen en de betekenis stilletjes verschuiven.
  - **`c-activity` en `c-activity-item` staan qua vorm dichter bij een postvakregel en bij een tijdlijnregel dan `c-table`.** Een activity-item draagt precies de velden die een gebeurtenis heeft: actor, actie, onderwerp (`res`), tijdstip (`at`) en een link. Een tijdlijn en een postvak zijn lijsten met regels, geen rasters met kolommen; kies de lijstvorm, tenzij de filters op type en gelezen/ongelezen een kolomindeling afdwingen.
  - **`c-badge` bestaat wel, maar wordt hier nog nergens gebruikt.** Nul treffers op `<c-badge` in `opi/templates_lotc/`; in de catalogus staat hij als "Small count or notification badge" met de standen default/info/success/warning/error. De conclusie blijft dus dat de teller in de kop een bestaande component is, maar hij is voor dit project nieuw: reken op een rondje vormcontrole in de proefopstelling in plaats van kopieerwerk van een bestaande pagina.
- **De regels.** `features/lotc-bouwlijn.md`: attributen in kebab-case, samenstellingen krijgen kinderen in plaats van data-props, Jinja niet op attribuutpositie. En: nooit een `{# ... #}`-commentaar BINNEN een componenttag.
- **Het htmx-patroon voor verversen.** Ligt er in twee vormen, en het verschil tussen die twee is voor dit ontwerp belangrijker dan de snelheid. Een lopende taak vervangt zichzelf met een kale tijdklok: `hx-trigger="every 2s"` (`opi/templates_lotc/partials/task_progress_fragment.html.j2:34`). Dat mag daar, want dat venster bestaat alleen zolang de taak loopt. Het blok dat vanzelf bijwerkt gebruikt **bewust geen tijdklok**: `opi/templates_lotc/bg/project-tabs.html.j2:987` luistert met `hx-trigger="intersect once, zad-metingen-ververs"` op een **eigen gebeurtenis**, en een scriptje eronder (`TUSSENPOOS = 60000`, regels 993-1011) vuurt die gebeurtenis elke minuut, maar keert meteen terug zolang het tabblad onzichtbaar is (`if (document.hidden || typeof htmx === 'undefined') return;`), met daarnaast een haak op `visibilitychange` die ververst zodra je terugkomt. Het commentaar op de regels 975-986 legt uit waarom die vorm er staat en de kale vorm niet:
  - een htmx-tijdklok blijft doorpeilen als het tabblad naar de achtergrond gaat, dus een tabblad dat een dag openstaat bevraagt een dag lang elke minuut de server;
  - en het voor de hand liggende lapmiddel daarvoor, een triggerfilter `every 60s [conditie]`, **kan hier niet**: htmx bouwt zo'n conditie met de `Function`-constructor en de Content-Security-Policy van deze applicatie staat geen `unsafe-eval` toe (`opi/middleware/security_headers.py:56`: `script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net`, en `unsafe-eval` staat er niet bij), dus de conditie zou stil nooit waar worden. Gemeten in RC-91.
- **De proefopstelling.** `/lotc/bg/<pagina>` met verzonnen gegevens uit `opi/web/lotc_fixtures.py`, zodat je vorm kunt kiezen zonder cluster.

**Let op**: het startpunt van de opdracht verwijst naar een `ROOS_CLAUDE_REFERENCE.md` op een lokaal pad. Dat pad bestaat hier niet, en dat klopt: `jinja-roos-components` is sinds RC-67 uit het project verdwenen (`features/roos-eruit.md`), en `CLAUDE.md` zegt het ook met zoveel woorden ("de oude ROOS-referentie is met de bibliotheek verdwenen"). De geldende referentie is `features/lotc-bouwlijn.md` plus `request_for_components.md` voor wat het thema nog niet kan.

### De verversingsweg: peilen vanuit de browser, en niet de websocket

Er is een websocket-router (`opi/api/logs_websocket_router.py`, 926 regels) met sessie-authenticatie, Origin-controle, verbindingslimieten en snelheidsbegrenzing. Hij is goed gebouwd en het is verleidelijk om hem te hergebruiken.

**Doe het niet, en de reden staat in het bestand zelf.** In de kop: "Connection limits are per-worker. For true global limits across workers, use Redis or a shared state backend." Een websocket voor logs is een verbinding die iemand bewust opent, kijkt, en sluit. Een teller in de kop is een verbinding die elke ingelogde gebruiker op elke pagina permanent openhoudt. Dat is een andere orde: van enkele gelijktijdige verbindingen naar een per open tabblad van iedereen, met per werker een eigen boekhouding.

**Aanbeveling: peilen vanuit de browser met een minuutcadans, en wel in de zichtbaarheidsbewuste vorm die er al ligt.** Dus niet `hx-trigger="every 60s"` op de teller, maar de vorm van `opi/templates_lotc/bg/project-tabs.html.j2:987`: `hx-trigger` op een eigen gebeurtenis (VOORSTEL: `zad-meldingen-ververs`), een scriptje in de schil met een `setInterval` van 60000 dat die gebeurtenis vuurt en meteen terugkeert zolang `document.hidden` waar is, plus een haak op `visibilitychange` die een keer ververst zodra het tabblad weer op de voorgrond komt.

Waarom de kale tijdklok hier juist niet mag, terwijl hij bij het taakvenster wel mag: de teller komt in `base_lotc.html.j2`, dus op **elke pagina van elke ingelogde gebruiker in elk open tabblad**, en hij blijft daar staan zolang die sessie duurt. Dat is precies de last waarvoor het bestaande blok de kale vorm heeft afgewezen, en dat blok stond nog maar op een tabblad van een pagina. Een vergeten tabblad met de teller erin zou anders een etmaal lang 1440 verzoeken kosten zonder dat er iemand kijkt. En de vluchtroute die je zou willen nemen, `every 60s [document.visibilityState === 'visible']`, is er niet: de CSP van deze applicatie verbiedt `unsafe-eval` en htmx bouwt zo'n conditie met de `Function`-constructor, dus hij faalt stil (RC-91). Wie dat niet weet bouwt hem, ziet geen fout, en denkt dat het werkt.

Met de zichtbaarheidshaak kost het een `hx-get` per minuut per **zichtbaar** tabblad, het werkt over meerdere werkers heen zonder gedeelde toestand, en het overleeft een herstart van OPI zonder dat er iets opnieuw verbonden moet worden. Een melding die een minuut later binnenkomt is geen probleem: dit is geen chat. En het terugkeergedrag is hier eerder een voordeel dan een concessie: op het moment dat iemand naar het tabblad terugschakelt staat de teller meteen goed, in plaats van tot de volgende tik verouderd te zijn.

Voor de postvakpagina zelf mag het sneller (VOORSTEL: 10 seconden) en mag de kale tijdklok wel, om dezelfde reden als bij `opi/templates_lotc/bg/_tasks.html.j2:28` (daar `every 5s` met `hx-swap="outerHTML"`): dat is een pagina die iemand bewust openzet, niet iets wat overal meereist. Wie ook daar netjes wil zijn, hangt er dezelfde zichtbaarheidshaak onder; verplicht is het niet. De tijdlijn op een projectpagina hoeft helemaal niet te verversen: die kijk je in en dan is hij klaar.

### Wat blokkeert

Niets fundamenteels. Twee dingen om op te letten:

- **De teller staat op elke pagina**, dus de bevraging erachter moet goedkoop zijn. Dat is precies waarvoor de partiele index `idx_notification_deliveries_unread` in het datamodel staat.
- **De proefopstelling `/lotc/*` is publiek** en zit in de release-image (`features/lotc-bouwlijn.md`). Een postvak- of tijdlijnfixture mag dus uitsluitend zichtbaar verzonnen waarden dragen, geen echte projectnamen en zeker geen echte e-mailadressen.

---

## Kanaal 2: de API

### Wat er nodig is

In de vorm van `opi/api/v2`, met getypeerde Pydantic-modellen zoals in `opi/api/task_models.py` en `opi/api/v2/models.py`. VOORSTEL:

| Methode en pad (VOORSTEL) | Wat het doet |
|---|---|
| `GET /api/v2/notifications` | het postvak van de aanroeper; filters op `unread`, `category`, `project`, `since`; paginering |
| `GET /api/v2/notifications/unread-count` | alleen het getal, want dat is de vaakste vraag en die hoort niet de hele lijst op te halen |
| `POST /api/v2/notifications/{id}/read` | een melding als gelezen markeren |
| `POST /api/v2/notifications/read-all` | alles als gelezen markeren, met dezelfde filters als de lijst |
| `GET /api/v2/notifications/preferences` | de voorkeuren van de aanroeper, inclusief de standaard van zijn rol voor wat hij niet zelf heeft gezet |
| `PUT /api/v2/notifications/preferences` | de voorkeuren schrijven |
| `GET /api/v2/projects/{p}/events` | de tijdlijn van een project; filters op `since`, `category`, `deployment`; paginering |

**Autorisatie, en let op: hier zitten twee verschillende regels naast elkaar.** De eerste zes gaan over "de aanroeper", en dat is nieuw in deze API. De projectroutes in `opi/api/v2/router.py` autoriseren op projectlidmaatschap of op de `X-API-Key` van het project. Die sleutel identificeert een PROJECT, geen mens, dus hij kan voor een postvak niet werken: een postvak dat op een projectsleutel opvraagbaar is, geeft de meldingen van een persoon aan iedereen die die sleutel heeft.

**De juiste weg voor de eerste zes staat er al.** `opi/api/user_token_auth.py` is precies hiervoor gebouwd, en de docstring zegt het zelf: "The rest of this API authenticates per project: an X-API-Key that belongs to one project and can do nothing outside it." Het is een bearer-token uit de SSO, met handtekening, uitgever, doelgroep en vervaldatum geverifieerd, en het levert een IDENTITEIT. De web-UI gebruikt daarnaast de sessie. Die twee zijn de authenticatie voor het postvak; de projectsleutel is dat expliciet niet.

**De zevende ligt andersom.** De projecttijdlijn gaat over een project en niet over een persoon, dus daar is de `X-API-Key` juist wel de goede sleutel, en dat is precies wat publiek C nodig heeft. Dat verschil moet in de code zichtbaar zijn en niet per ongeluk ontstaan.

**En twee dingen die op de eerste zes zelf horen te staan, want anders worden ze verkeerd gebouwd.** Ten eerste: `POST /api/v2/notifications/{id}/read` en `POST /api/v2/notifications/read-all` werken altijd op de ontvangerrij van DE AANROEPER en niet op een melding-id op zichzelf. De `id` in het pad is de rij uit `notification_deliveries` (deel 2 punt 7), en de `UPDATE` draagt daarnaast altijd het filter op de recipient uit de authenticatie; een id dat van iemand anders is levert dezelfde 404 op als een id dat niet bestaat, zodat het bestaan van andermans melding niet af te tasten is. Zonder die regel wordt dit een IDOR: de identiteit bepaalt dan wel wie er mag lezen maar niet op wiens rij er wordt geschreven. Ten tweede: `GET /api/v2/notifications/unread-count` is de route die de teller uit Kanaal 1 vanaf elk zichtbaar tabblad elke minuut bevraagt, en vanaf de postvakpagina elke tien seconden, dus hij is per definitie de vaakst geraakte route van deze API. Hij krijgt daarom een snelheidsbegrenzing, en die hoeft niet bedacht te worden: `IPRateLimiter` (`opi/api/router.py:64`) ligt er al en wordt op dezelfde manier gebruikt door `subdomain_check_rate_limiter` (`opi/api/router.py:338`, 30 aanvragen per minuut met een burst van 10). VOORSTEL hier: 60 per minuut, want de eigen cadans van Kanaal 1 zit met een handvol zichtbare tabbladen en de postvakpagina erbij ruim daaronder, en een scriptje dat de route in een lus trekt zit er ruim boven. Begrenzen op de AANROEPER en niet alleen op het IP-adres, want achter een kantoornetwerk delen collega's er een.

### Wat er al ligt

- **De vorm.** `opi/api/v2/models.py` en `opi/api/task_models.py` laten precies zien hoe een antwoordmodel er hier uitziet, inclusief `StrEnum`-velden en generieke responses (`TaskResponse[TResult]`, `task_models.py:416`).
- **De persoonsgebonden authenticatie.** `opi/api/user_token_auth.py`.
- **De OpenAPI-documentatie is zelfbeschrijvend** en wordt op `/openapi.json` geserveerd, dus de zad-cli en een agent zien nieuwe endpoints vanzelf.

### Wat blokkeert

Niets.

### Het INKOMENDE endpoint

Wat meteen zinnig is en goedkoop: **een gebeurtenis van buiten kunnen aannemen.** Een GitHub Action die een scanbevinding meldt, of een monitoringsysteem, die als gebeurtenis in ZAD landt. Dat is een route die een CloudEvent aanneemt, valideert en als gebeurtenis wegschrijft. Het maakt drie regels uit de inventarisatie die vandaag "bestaat nog niet" zijn haalbaar zonder dat ZAD er iets voor hoeft waar te nemen, en dat zijn de drie die deel 1 zelf aanwijst: de beveiligingsscan (deel 1 `:306`), de verouderde image (deel 1 `:307`) en het verlopende certificaat (deel 1 `:308`). Dat zijn de enige drie catalogusrijen waarvan de kanaalkolom niet naar een kanaal maar naar dit endpoint doorschuift ("(zie hieronder)"), en het is precies het drietal waarover deel 1 `:312` "Over de laatste drie regels" kopt. Alle drie dragen publiek `B`, dus alle drie komen ze onder ontwerpregel 6 hieronder ook werkelijk langs deze route binnen. "Onderhoud is gepland" (deel 1 `:305`) hoort hier uitdrukkelijk niet bij: dat is geen regel waarvoor deze route bestaat, en ontwerpregel 6 legt uit waarom hij er niet langs mag en langs welke weg hij dan wel wordt neergezet.

Een waarschuwing die erbij hoort en die makkelijk wordt overgeslagen: dat endpoint is een SCHRIJFweg vanaf buiten naar een tabel die een audittrail moet kunnen worden, en die tabel is alleen-invoegen. Er is dus geen correctiepad, en dat is geen omissie: 8.15.05 en 8.15.06 uit deel 2 paragraaf 6 zijn juist de reden dat die tabel er geen heeft, dus wat er verkeerd in komt blijft erin staan tot de retentieklok hem opruimt. De ontwerpregels hieronder horen daarom in het ontwerp te staan voor er iemand aan begint. **Het zijn er zes**, en de laatste drie gaan over wie wat mag zetten en niet over de vorm. Zie de fasering, de laatste fase.

1. **Authenticatie, dus geen anonieme POST.** Welke wegen er zijn staat niet hier maar in ontwerpregel 4 en 6, want op deze route bepaalt de weg ook de scope en daarmee wat er aangeboden mag worden.
2. **Een beperking op welke `type`-waarden er van buiten mogen komen.** De gesloten enum uit deel 2 punt 7 is de catalogus van alles wat ZAD zelf schrijft; wat van buiten mag is een deelverzameling daarvan, en die staat apart opgeschreven. Ontwerpregel 6 hieronder zegt hoe die deelverzameling wordt begrensd.
3. **Een eigen `actor_kind = external`.** Dat is de vijfde waarde die deel 2 punt 7 (`:583`) voor deze route openlaat, en er bestaat geen rij met die waarde zolang deze route niet bestaat.
4. **De scope en `source` volgen uit de autorisatie van de aanroeper en nooit uit de body.** `subject`, `project`, `deployment` en `component` worden niet overgenomen uit het aangeboden CloudEvent. De projectnaam hoort in het PAD te staan en de sleutel wordt daartegen gelegd, precies zoals `validate_api_token` dat vandaag al doet: "ALWAYS validates that the API key matches the project_name from the route" (`opi/api/endpoint_util.py:33`, de docstring van de decorator op `:28`, het uitlezen uit de routeparameters op `:56` en de vergelijking op `:66-68`, zelf nagelezen). VOORSTEL: `POST /api/v2/projects/{project_name}/events` voor de projectscope en `POST /api/v2/events` voor de platformscope. Zonder die binding landt een van buiten aangeboden gebeurtenis op de tijdlijn, in de postvakken en vanaf fase 6 op de webhook van elk project dat de aanbieder in de body noemt, en de twee BEREIKgrendels uit ontwerpregel 3 van Kanaal 5 houden dat niet tegen: die filteren op de scope, en die scope is dan door de aanbieder zelf gezet. `source` valt onder dezelfde regel en om een andere reden: dat veld is de identiteit van het systeem dat de gebeurtenis UITGEEFT, en dat is ZAD zelf (`urn:nld:oin:<OIN>:systeem:zad-<cluster>`, deel 2 punt 7). Een aangeboden `source` wordt genegeerd en komt in geen enkele kolom terecht. Wie de aanbieder was hoort in `data`, in een veld dat de server zelf samenstelt, en niet in een profielattribuut. **Welke sleutel de platformscope van `POST /api/v2/events` draagt, want dat is tot hier alleen terloops genoemd.** Dat is de `ADMIN_API_KEY` van `validate_admin_api_key` (`opi/api/endpoint_util.py:103`), dezelfde gedeelde platformsleutel die de onderhoudsroutes vandaag al dragen, en dus geen nieuw geheim erbij. Ontwerpregel 6 sluit precies die sleutel uit voor de platformMEDEDELING, en dat is geen tegenspraak maar het verschil dat die regel maakt: een gedeeld geheim zonder rol en zonder persoon mag niet uitzenden naar iedereen, en hier mag het wel omdat ontwerpregel 6 de toegestane soorten op deze scope beperkt tot publiek uitsluitend `B`. De straal van een gelekte sleutel is daarmee begrensd tot ruis in het postvak van de platformbeheerders die de sleutel toch al beheren, en niet tot een bericht onder de naam van ZAD bij elke gebruiker. Twee eigenschappen horen erbij. Hij is niet altijd gezet, en dan weigert de decorator met 501 in plaats van door te laten (`opi/api/endpoint_util.py:127-131`), dus deze route is fail-closed zolang het geheim ontbreekt. En hij draagt geen persoon, dus de rij krijgt `actor = NULL` bij `actor_kind = external` volgens ontwerpregel 5, en wie de sleutel gebruikte is niet uit de rij af te lezen. Dat laatste is de prijs van een gedeelde sleutel, en het is de reden dat de aanbieder in `data` terechtkomt, in het veld dat de server zelf samenstelt.
5. **`actor`, `actor_kind`, `time` en `origin` worden door de server gezet en zijn niet door de aanroeper te kiezen.** Deel 2 punt 7 (`:583`) schuift de actorkeuze voor deze route uitdrukkelijk naar dit document door; hier staat zij. **Gekozen: `actor` volgt de WEG en niet de body**, dezelfde regel als daar. Langs een projectsleutel of een gedeelde platformsleutel blijft `actor` op `NULL`, want zo'n sleutel identificeert een project of een systeem en niet de handelende partij; langs een bearer-token of de sessie is het het geverifieerde e-mailadres uit `validate_user_token` (`opi/api/user_token_auth.py:260`). `actor_kind` is op deze route altijd `external` en wordt niet uit de body gelezen. `time` is de ontvangsttijd van de server, want met een aangeboden tijdstip schuift een aanbieder zijn gebeurtenis vooruit of achteruit in de tijdlijn en daarmee ook langs de twee retentieklokken uit deel 2 paragraaf 6. `origin` is de netwerklocatie die de server waarneemt, en wat dat precies is staat als regel bij de `origin`-kolom in deel 2 punt 7 (`:582`): het adres van de peer van de verbinding, met de `X-Forwarded-For` alleen als die peer de bekende ingress is en dan de hop die die proxy zelf toevoegde, nooit de meest linkse waarde. Dat is hier dragend en niet decoratief, want deze route neemt van buiten aan: zonder die regel kiest de aanbieder naast zijn eigen gebeurtenis ook zijn eigen oorsprong. Deel 2 (`:727`) tekent erbij aan wat er buiten de gebeurtenissencode voor nodig is. Zonder deze regel is het Actor-veld dat BIO2 8.15.01 vraagt (deel 2 paragraaf 6) op de enige route die van buiten schrijft van buiten te zetten, en dan wijst het spoor precies aan wie de aanbieder wil dat het aanwijst.
6. **Geen van buiten aangeboden soort met een publiek dat breder is dan de scope waarvoor de aanroeper is geautoriseerd.** De toegestane `type`-lijst uit ontwerpregel 2 is daarmee geen enkele lijst maar een lijst PER scope. Op de projectsleutel mag alleen een soort die een project draagt en waarvan het publiek binnen dat project blijft. Op de platformscope mag alleen een soort met publiek uitsluitend `B`, dus een die alleen bij platformbeheerders landt: dat dekt alle drie de regels waarvoor deze route bestaat, de beveiligingsscan (deel 1 `:306`), de verouderde image (deel 1 `:307`) en het verlopende certificaat (deel 1 `:308`), want die dragen alle drie publiek `B`, en het houdt de straal van een gedeelde platformsleutel binnen het postvak van de beheerders zelf. **Wat de regel juist buiten houdt is een ander geval, en dat is het scherpste geval dat er is.** "Onderhoud is gepland" (deel 1 `:305`) heeft publiek `A en B` en als standaardkanaal postvak plus mail, dus vanaf fase 8 is dat een uitzending naar elke gebruiker onder de naam van ZAD, met vrije tekst in `summary`. Dat "elke gebruiker" is geen aanname over de bezorging maar de regel zelf: deel 2 punt 4 maakt type 11 uitdrukkelijk de ene soort zonder project die toch bij iedereen wordt neergelegd, met het platformregister als ontvangerslijst in plaats van het lidmaatschap van een project, en met een eigen `reason`-waarde `platform-announcement` in punt 7. Zonder die uitzondering zou de mededeling alleen bij de platformbeheerders landen, en dan zou deze ontwerpregel een gevaar weren dat er niet is en zouden deel 1 `:523` en het rolprofiel van het projectlid op `:377` hierboven in dit document niet te bouwen zijn. Hij is geen van de drie regels waarvoor deze route bestaat, maar hij ligt er wel tegenaan, want een aanbieder die deze soort mocht zetten zou vanaf een sleutel naar iedereen kunnen uitzenden. **Gekozen: die soort komt niet van buiten binnen**, en dat geldt voor zijn hele type: type 11 `platform-mededeling` (deel 1 `:523`, publiek `A en B`) omvat naast onderhoud ook "er is een nieuwe release van het platform" (deel 1 `:304`) en de overige clusterbrede berichten, en geen van die soorten is van buiten aan te bieden. Ze worden neergezet door een platformbeheerder met een IDENTITEIT, langs de sessie of `opi/api/user_token_auth.py` met `UserService.is_platform_admin` (`opi/services/user_service.py:279`), en die rij draagt dan `actor_kind = human` met de actor erbij. Dat kan niet dezelfde route zijn als deze, want ontwerpregel 5 pint `actor_kind` op dit endpoint op `external` en leest hem niet uit de body; het is dus een eigen route. VOORSTEL: `POST /api/v2/platform-announcements`, uitsluitend langs die twee identiteitswegen, met de soort uit type 11 en `summary` als tekst, en hij staat in fase 9 naast dit endpoint omdat hij de keerzijde van deze ontwerpregel is. Uitdrukkelijk niet op de `X-API-Key` van een project en ook niet op de `ADMIN_API_KEY` van `validate_admin_api_key` (`opi/api/endpoint_util.py:103`), want dat is opnieuw een gedeeld geheim zonder rol en zonder persoon, en dat is dezelfde grond waarop ontwerpregel 5 van Kanaal 5 de projectsleutel uitsluit. En op dit endpoint blijft `summary` ook binnen de toegestane lijst van buiten aangeleverde vrije tekst: hij wordt als tekst weergegeven en nooit als opmaak, en hij valt onder dezelfde redactie op de SCHRIJFweg die deel 2 punt 4 voor elke gebeurtenis eist.

---

## Kanaal 3: e-mail

**Dit is de valkuil van deze opdracht, en hij is anders dan de opdracht vermoedt.**

### Wat er nodig is

Een verzendweg: OPI moet een SMTP-bericht kunnen aanbieden aan de relay.

### Wat er al ligt, en dat is meer dan verwacht

De opdracht stelt dat OPI vandaag zelf geen mail verstuurt, en dat klopt: er is **nergens in `opi/` een import van `smtplib` of `aiosmtplib`**. Gemeten met een grep over de hele pakketboom. `opi/connectors/mail.py` praat uitsluitend met de beheer-API van de relay (principals aanmaken, limieten zetten, afzendernamen schrijven) en verstuurt niets.

Maar de opdracht stelt ook dat "het platform als klant van zijn eigen dienst met een eigen account op de relay voor de hand ligt". **Dat account bestaat al.** Dit is de belangrijkste vondst van dit deel:

| Wat | Waar | Stand |
|---|---|---|
| De accountnaam-instelling | `MAIL_PLATFORM_ACCOUNT = "zad-platform"`, `opi/core/config.py:407` | bestaat |
| Het dagbudget | `MAIL_PLATFORM_MESSAGES_PER_DAY = 2000`, `opi/core/config.py:408` | bestaat |
| Het geheim met de inloggegevens | `MAIL_PLATFORM_SECRET_NAME = "zad-platform-mail-account"`, `opi/core/config.py:413` | bestaat |
| Het aanmaken bij het opstarten | `MailManager.ensure_platform_account()`, `opi/manager/mail_manager.py:248`, aangeroepen via `ensure_platform_mail_account()` in `opi/core/startup.py:405` | bestaat, niet-kritiek bij het opstarten |
| Het wachtwoord | wordt gegenereerd bij de eerste ontmoeting met een draaiende relay en bewaard in een Secret in de eigen namespace | bestaat, idempotent |
| Het netwerkpad | egressregel naar `rig-prd-ron` op poort 587, `bootstrap/rig-system/kustomize/operations-manager/overlays/odcn-production/network-policy.yaml` | bestaat |

De regel in dat netwerkbeleid draagt zelfs het commentaar: *"587 om zelf post aan te bieden (ZAD verstuurt uitnodigingen en meldingen als gewoon account)"*. En de docstring bij `ensure_platform_mail_account` zegt: *"What it does block is password reset and invite mail"*.

**Wat er dus ontbreekt is precies een ding: de SMTP-client.** Een module die de inloggegevens uit het Secret leest, verbinding maakt met de relay op 587, authenticeert en een bericht aanbiedt. Dat is klein werk, en het is precies het soort werk waar de connectorregel van dit project op van toepassing is: het hoort een connector te worden (`opi/connectors/`) en geen losse aanroep ergens in een service.

### De afzender is een identiteitsbeslissing en geen instelling

De relay dwingt de `From:` af. Voor een project wordt dat `noreply-rijksapp+<project>@rijksoverheid.nl` (`generate_mail_sender_address`, `opi/utils/naming.py:742`), inclusief de weergavenaam uit de projectconfiguratie.

Voor het platformaccount is dat bewust anders. `ensure_platform_account` zet het adres op `MailManager._sender_address(cluster, None)`: het **kale** adres, dus `noreply-rijksapp@rijksoverheid.nl`, en `from_name=""`, dus **zonder weergavenaam**. Het commentaar erbij: "ZAD is not a project, so there is no project name to put in the plus part and no project configuration to take a display name from."

**Gevolg, en dit is de beslissing die de opdrachtgever moet nemen.** Een melding van ZAD komt aan als een berichtje van `noreply-rijksapp@rijksoverheid.nl`, zonder naam ervoor. Dat is technisch juist en menselijk mager: de ontvanger ziet niet dat het van het ZAD-portaal komt tot hij het onderwerp leest. Drie opties:

1. **Laten staan.** Nul werk. De herkenning moet uit het onderwerp komen ("ZAD: je deployment ...").
2. **Een weergavenaam zetten voor het platformaccount.** De machinerie bestaat al (`set_sender_name`, `opi/connectors/mail.py:269`), dus dit is een regel: het platformaccount krijgt `from_name = "ZAD"` of "Rijksapps ZAD". Het adres blijft het kale adres.
3. **Een eigen adres voor het platform.** Bijvoorbeeld `noreply-rijksapp+zad@...`. Dat is een afspraak met het mailteam en het botst met de plusdeel-conventie die "plusdeel = project" betekent (`opi/manager/mail_manager.py:56`, `MailAccountNameError`, dat de botsing tussen platform- en projectnaamruimte uitdrukkelijk weert).

**Aanbeveling: optie 2.** Een weergavenaam, geen nieuw adres. Herkenbaar in de postbus, geen gesprek met het mailteam, en de naamruimteregel blijft intact.

### Wat blokkeert, en dit is het echte werk

Vier dingen, alle vier al gemeten en vastgelegd in `plans/mail-vervolgpunten.md` en `TODO.md` punt 26. Ze staan hier omdat ze de bruikbaarheid van dit kanaal bepalen, niet omdat dit traject ze moet oplossen. Ze zijn samen de reden dat mail laat in de fasering staat: **een meldsysteem opzetten op een keten die stil faalt, is een meldsysteem dat stil faalt.**

1. **De relay staat op productie nog niet aan.** Punt 5 van `mail-vervolgpunten.md`: de manifesten en geheimen zijn klaar, wat rest zijn afspraken en twee regels configuratie (`MAIL_RELAY_API_URL` aan in de OPI-overlay, de Application `ron-infrastructure` aan). Zonder dat is `MAIL_RELAY_API_URL` leeg, en dan maakt `ensure_platform_account` netjes geen account aan en meldt het in het log.
2. **De upstream weigert ontvangers buiten `rijksoverheid.nl`.** Punt 8, gemeten op 21 augustus 2026: naar een adres bij rijksoverheid.nl volgde `250 ok`, naar een gmail-adres `550 #5.1.0 Address rejected` op de RCPT TO. **Dat raakt dit traject direct.** In `bootstrap/rig-system/kustomize/operations-manager/overlays/odcn-production/configmap.yaml:45` staat vandaag een `ALLOWED_EMAILS` met zes adressen, waarvan er een op `odc-noord.nl` staat. Die persoon is met de huidige afspraak per e-mail niet te bereiken. Een meldingssysteem dat ervan uitgaat dat mail werkt voor iedereen, klopt dus niet.
3. **Bounces verdwijnen stil.** Punt 10: mislukt een bezorging, dan maakt de relay een DSN en stuurt die naar het envelope-adres, en de upstream weigert dat adres ALS ONTVANGER met dezelfde 550. De relay noteert "discarding message after double bounce" en gooit hem weg; de relaylog bewaart drie uur. **Voor meldingen betekent dat: het kanaal kan zeggen dat het is gelukt terwijl het bericht nergens aankwam.** Wat de outbox als `sent` markeert, is "de relay heeft hem aangenomen", en dat is niet hetzelfde als afgeleverd. Dat moet in de UI ook zo staan, en niet als "verstuurd".
4. **De MTA-STS-lookup kost twee minuten per bericht naar een domein dat het publiceert.** Punt 9, gemeten: 131 seconden naar gmail.com. Raakt ons alleen als er ooit buiten `rijksoverheid.nl` gemaild wordt, en dat kan vandaag toch niet. Wel relevant voor de time-out van de outboxplanner: die moet royaal zijn, anders markeert hij een bezorging als mislukt terwijl hij nog loopt.

**Conclusie voor de fasering: e-mail is laat.** Het account is er, het netwerkpad is er, de client is klein werk. Maar het kanaal is pas eerlijk als de relay op productie aanstaat en als duidelijk is wat er met een niet-bezorgbaar adres gebeurt. Tot die tijd zijn de tijdlijn en het postvak het kanaal, en die werken voor iedereen. De vier punten zijn de VOORWAARDE en niet de context.

### Afmelden, samenvatten, en hoeveel er in een mail hoort

**Afmelden.** Elke mail draagt een regel met een link naar het voorkeurenscherm. Geen `List-Unsubscribe`-header met een eigen afmeldweg: die is bedoeld voor bulkpost aan mensen die zich ooit aanmeldden, en dit is werkverkeer aan collega's. De weg naar "zet dit type uit" is het scherm, en dat scherm is een klik ver en veroorzaakt geen commit; zie deel 2, punt 6, waar dat ook een AVG-argument is en niet alleen gemak.

**Samenvatting versus meteen.** Beide, per type instelbaar, met een bruikbare standaard:

- **Meteen** voor wat een handeling vraagt: een storing, een aanvraag die op jou wacht, een onomkeerbare ingreep.
- **Dagelijkse samenvatting** voor de rest. Een mail per dag met wat er die dag is bijgekomen. Dat is ook de rem op het uitbarstingsprobleem: twintig gebeurtenissen op een slechte middag worden een mail.

De samenvatting is in het datamodel goedkoop: de outboxrijen krijgen `next_attempt_at` op het verzendmoment van de samenvatting in plaats van op nu, en de verzender bundelt wat voor dezelfde ontvanger klaarstaat. Geen tweede mechanisme. De bovengrens per ontvanger per dag uit deel 2, punt 3, hangt eraan als noodrem.

**Hoeveel inhoud.** De aanname in de opdracht is: zo min mogelijk, met een link terug, want de mail verlaat ons vertrouwensgebied. **Die aanname klopt en wordt hier niet weerlegd, maar hij is te streng als je hem letterlijk neemt.** Een mail met alleen "er is iets gebeurd, klik hier" is onbruikbaar: de ontvanger moet kunnen beslissen of hij nu moet handelen of pas maandag, en daarvoor moet hij weten wat er gebeurde en waarover.

Voorstel voor de grens:

| Wel in de mail | Niet in de mail |
|---|---|
| wat er gebeurde, in een zin (de `summary`-kolom) | logregels, stacktraces, foutmeldingen van de applicatie |
| de projectnaam en de deploymentnaam | omgevingsvariabelen, geheimen, verbindingsgegevens |
| de ernst en het tijdstip | e-mailadressen van andere betrokkenen |
| waarom je hem krijgt (de `reason`-kolom) | de inhoud van het projectbestand |
| een link naar het onderwerp in ZAD | alles waarvoor je in ZAD moet zijn ingelogd om het te zien |

De onderliggende regel: **wat in de mail staat, is wat er in een openbaar berichtenlogboek zou mogen staan.** Een projectnaam mag dat; een foutmelding uit een container niet, want die kan alles bevatten. Dat is dezelfde regel die het CloudEvents-profiel op de context-attributen legt en dezelfde die deel 2, punt 4 op de schrijfweg legt, en het is prettig dat die drie samenvallen.

---

## Kanaal 4: Mattermost

### Wat er nodig is

Vier dingen, en het derde is het echte werk:

1. **Een bereikbare Mattermost**, en dus het antwoord op de vraag of hij op het internet staat of achter het Rijksnetwerk.
2. **Een bot met een token**, in een Secret in de namespace van OPI, en een connector die er privéberichten mee stuurt (`opi/connectors/mattermost.py`, VOORSTEL, want elke externe aanroep gaat in dit project door een connector).
3. **Een koppeling tussen een ZAD-gebruiker en een Mattermost-account**, met bewijs dat de persoon werkelijk over dat account beschikt.
4. **Een netwerkregel**, of een tussenstation als de Mattermost achter het Rijksnetwerk staat.

### Wat er al ligt: niets

Een grep over de hele repository op `mattermost`, hoofdletterongevoelig, over Python, Markdown, YAML en Jinja: **nul treffers.** Er is geen connector, geen instelling, geen netwerkregel, geen notitie en geen post-mortem. Dit kanaal begint bij nul.

### Wat blokkeert: welke Mattermost is dit, en kunnen we erbij

**Dat is niet uit de code te beantwoorden en het is de eerste vraag die beantwoord moet worden**, want het antwoord bepaalt of dit kanaal überhaupt kan.

Wat wel vaststaat over het netwerk, en dat is genoeg om de vraag scherp te stellen:

- De namespace van OPI op productie draagt `egress.projectcalico.org/egressGatewayPolicy: "internet"` (`bootstrap/rig-system/kustomize/overlays/odcn-production/namespace.yaml:7`).
- Het netwerkbeleid van OPI laat uitgaand verkeer toe op poort 443 naar elke bestemming (`bootstrap/rig-system/kustomize/operations-manager/overlays/odcn-production/network-policy.yaml`).

**Dus: een Mattermost op het internet is bereikbaar.** Zonder nieuwe regel, zonder gesprek.

**En een Mattermost binnen het Rijksnetwerk is dat niet, en dat is een harde blokkade.** `docs/ron-koppeling.md:70`: de annotatie neemt **een** waarde, `internet` of een klantgateway zoals `rig-ron`, en de een vervangt de ander. `plans/mailrelay.md:258` heeft dat gemeten en getrokken tot de conclusie: RON aanzetten op `rig-prd-operations` kost daar het internet, en daarmee ArgoCD, de registry en Keycloak. Precies daarom draait de mailrelay in een EIGEN namespace (`rig-prd-ron`) en niet naast OPI.

Als de Mattermost op RON staat, is de oplossing dus dezelfde als bij de mail: een klein tussenstation in een eigen namespace met `rig-ron`, waar OPI intern naartoe praat. Dat is geen regel erbij, dat is een component erbij.

**Wat de opdrachtgever moet uitzoeken, in deze volgorde:**

1. Welke Mattermost is het (een URL)?
2. Staat die op het internet of achter het Rijksnetwerk?
3. Mogen wij er een bot registreren, en wie beheert dat token?

Vraag 2 is de blokkade; de andere twee zijn afspraken.

### Het echte vraagstuk is niet versturen maar koppelen

Versturen is een POST. **De vraag is: hoe weet ZAD welk Mattermost-account bij een persoon hoort?** ZAD kent mensen op e-mailadres (uit Keycloak, uit de `users:`-lijst van een project, uit `ALLOWED_EMAILS`). Mattermost kent mensen op gebruikersnaam en op een eigen id.

Drie manieren, en de derde is de enige die schaalt:

1. **Zoeken op e-mailadres via de Mattermost-API.** De bot vraagt "welke gebruiker heeft dit adres". Werkt als de adressen overeenkomen. Nadeel: het vraagt een bot met leesrechten op de gebruikerslijst van de hele werkruimte, en dat is een stevig recht om te vragen voor een meldingsfunctie. En het faalt stil bij iemand die daar een ander adres gebruikt.
2. **De gebruiker vult zijn Mattermost-naam in het voorkeurenscherm in.** Eerlijk, maar onverifieerbaar: hij kan die van een ander invullen en dan gaan zijn meldingen daarheen.
3. **De gebruiker koppelt zichzelf, met bewijs.** ZAD toont een code, de gebruiker stuurt die in een privébericht naar de bot, de bot meldt het terug aan ZAD, en de koppeling staat. Omgekeerd kan ook: de bot stuurt de code, de gebruiker plakt hem in ZAD. Dat is hoe elke koppeling van dit type werkt, en het is de enige vorm waarin ZAD weet dat de persoon werkelijk over dat account beschikt.

**Aanbeveling: 3, en dat betekent een tabel `notification_channel_identities` (VOORSTEL) met `recipient`, `channel`, `external_id` en `verified_at`.** Die tabel staat bewust niet in het datamodel van deel 2: hij hoort bij dit kanaal en niet bij de kern.

### Een kanaalwebhook is geen persoonlijke melding

Dit moet expliciet, want het is de goedkope oplossing die zich als de echte voordoet. Het is ook niet hetzelfde als de webhook per project uit Kanaal 5, maar het deelt er wel de eigenschap mee die daar de twee zwaarste ontwerpregels oplevert: de bestemming wordt door een gebruiker gekozen.

Een **inkomende webhook** in Mattermost is een URL waar je een bericht naartoe POST, en dat bericht komt in een KANAAL. Dat is:

- geen persoonlijke melding (iedereen in het kanaal ziet alles van iedereen);
- niet te filteren met de voorkeuren van een persoon (er is geen persoon);
- een informatielek zodra de meldingen projectgegevens dragen en het kanaal breder is dan het project;
- wel binnen een half uur werkend.

Een **bot met een privébericht** is de vorm die de wens beschrijft: persoonlijk, per persoon in te stellen, en niet zichtbaar voor anderen. Het kost een botregistratie, een token in een Secret, en de koppeling hierboven.

**Aanbeveling: allebei, maar niet als alternatieven van elkaar.** De bot is het persoonlijke kanaal en hoort bij dit traject. De kanaalwebhook is iets anders: een PROJECTinstelling ("stuur meldingen over dit project ook naar ons teamkanaal"), waar het projectteam zelf zijn webhook-URL invult. Dat is een dienst in de catalogus en geen persoonlijk kanaal, en het is een prima tweede stap. Ze moeten alleen nooit in hetzelfde voorkeurenscherm terechtkomen, want dan gaat iemand ervan uit dat zijn persoonlijke instelling ook geldt voor wat er in het teamkanaal verschijnt. **Het is geen lichtere stap.** Omdat de bestemming door een gebruiker wordt gekozen valt dit kanaal, zodra het gebouwd wordt, onverkort onder ontwerpregel 4 (waar de aanvraag heen mag) en ontwerpregel 5 (wie hem mag neerzetten) van Kanaal 5 hieronder. Dat gaat niet vanzelf goed als hij een gewone catalogusdienst wordt: de configuratie van een dienst op projectniveau is met de `X-API-Key` van het project te schrijven (`PUT`, `DELETE` en `PATCH` op `/projects/{project_name}/services/{service_name}/config` staan alle drie in `validate_api_token`, `opi/api/v2/router.py:2903`, `:2915` en `:2935`, zelf nagelezen), en dat is precies de weg die ontwerpregel 5 voor de webhook per project uitsluit. De webhook-URL invullen moet dus ook hier een handeling van een projectbeheerder met een identiteit zijn.

---

## Kanaal 5: de webhook per project

### Wat er nodig is

Een bezorger met een herhaalbeleid, een geheim per abonnement, een abonnementstabel met het watermerk uit deel 2 punt 3, en uitgaand netwerkbeleid. De ontvanger is de eigen tooling van het project, en dat is precies de partij die vandaag al de `X-API-Key` van dat project houdt.

### Wat er al ligt

Niet de outbox uit deel 2, punt 2: die hangt via `delivery_id` aan een rij die voor EEN PERSOON is neergelegd, en een projectwebhook heeft geen persoon (deel 2, punt 7). Wat er wel ligt is het PATROON van die outbox, en dat is regel voor regel over te nemen op het abonnement: de pogingsteller, de exponentiële herhaling met een plafond, de claim per werker, en het opgeven met een gebeurtenis erbij. De bezorgstand hangt dan aan het abonnement en aan het watermerk `reported_through` uit deel 2, punt 3, en niet aan een ontvangerrij. Het netwerkpad ligt er ook, en let op wat dat wel en niet zegt: de namespace draagt `egress.projectcalico.org/egressGatewayPolicy: "internet"` en het eigen beleid van OPI laat uitgaand TCP/443 naar elke bestemming toe (`bootstrap/rig-system/kustomize/operations-manager/overlays/odcn-production/network-policy.yaml`, waar de eerste egressregel letterlijk `to: []` op poort 443 is, zelf nagelezen). Dat is het antwoord op de vraag of er infrastructuurwerk moet gebeuren, en het is het OMGEKEERDE antwoord op de vraag of dit veilig is: er ligt geen enkele grens tussen de bezorger en welke bestemming dan ook, inclusief `kubernetes.default.svc` en alles wat verder alleen vanuit het cluster op 443 bereikbaar is. Zie ontwerpregel 4.

### Wat blokkeert

Niets technisch, en dat is hier juist het risico: het enige dat dit kanaal begrenst zijn ontwerpregels, dus die moeten staan voordat iemand aan fase 6 begint. Het zijn er vijf, en de laatste twee gaan alleen over de veiligheid van het kanaal en niet over zijn vorm:

1. **Het geheim.** Een HMAC-handtekening over de body met een geheim per abonnement, zodat de ontvanger weet dat het bericht van ons komt. Dat geheim wordt AGE-versleuteld opgeslagen als het in het projectbestand belandt, en voor de databasetak geldt dezelfde eis en niet een lichtere: versleuteld met de AGE-sleutel van het project (`config/age-public-key` uit het projectbestand), nooit in klaartekst in een kolom. Er is een precedent om van te erven en dat is precies de vergelijkbare sleutel: de `X-API-Key` van een project wordt bij het aanmaken met diezelfde publieke sleutel versleuteld (`EncryptedAPIKeyGenerator` in `opi/forms/editables/generators.py:77-90`, met de docstring "Generate an API key and encrypt it with the project's public key" op `:78` en de versleuteling op `:89`). Hij wordt per ABONNEMENT gegenereerd en niet per project, zodat het intrekken van een abonnement ook het geheim intrekt; hij is na het aanmaken niet meer op te vragen maar alleen te vervangen; en hij komt niet in een gebeurtenis en niet in een logregel terecht, wat dezelfde regel is als deel 2 punt 4 voor elk ander geheim stelt.
2. **Waar het aan-uit-vinkje staat.** Zolang een webhook binnen het bestaande netwerkbeleid past, staat het abonnement volledig in de database (deel 2). Zodra een webhook een eigen NetworkPolicy nodig heeft, hoort het aan-uit-vinkje in het projectbestand, precies zoals `vlam` en `send-email` dat doen (`features/vlam-service.md`, `features/send-email.md`). De ontvanger-URL en het bezorgbeleid blijven dan nog steeds in de database. **Dat vinkje staat daarmee in een laag die met de `X-API-Key` te schrijven is, en dat botst niet met ontwerpregel 5 hieronder:** die regel gaat over het aanmaken, wijzigen en intrekken van het ABONNEMENT, dus over de bestemming, en die blijft in de database achter een identiteit. Het vinkje kan alleen het netwerkpad naar een al neergezette bestemming openen of sluiten en er geen nieuwe bijzetten, en het omzetten ervan is zelf een gebeurtenis van type 9, zodat ook het opnieuw openen aanwijsbaar blijft.
3. **Wat er in de body staat, en welke gebeurtenissen eruit gaan.** Dezelfde grens als bij de mail: de gebeurtenis in CloudEvents-VORM, met `subject` op de projectnaam en de actor in `data`, en nooit een ongefilterde foutmelding. `origin` gaat er NIET in mee, want de netwerklocatie is publiek `B` en de ontvanger hier is het project. Zie deel 2, punt 4. **In vorm, en niet als geldig CloudEvent.** Zolang de OIN openstaat draagt `source` een plaatshouder, en regel 1 van de beslechting "CloudEvents als intern opslagformaat" in deel 2 verbiedt dat zo'n record zich als CloudEvent aandient. Die beslechting zondert deze webhook uit van het exportverbod, want de ontvanger is de eigen tooling van het project en er is geen standaard in het spel die iets belooft, en zij noemt de voorwaarde: content-type `application/json` en niet `application/cloudevents+json`, en de webhookdocumentatie noemt de body CloudEvents-vormig. Daarmee wacht dit kanaal niet op de OIN uit openstaande beslissing 12; de exporteur in fase 9 wel. **En alleen de gebeurtenissen van DIT project.** Een abonnement hangt aan een project, dus de bezorger levert uitsluitend gebeurtenissen waarvan de projectscope dat project is, en de keuze van categorieen in `notification_subscriptions` is een filter BINNEN die scope en nooit een uitbreiding ervan. **Twee grendels dus, en niet een, en ze heten hier samen de BEREIKgrendels: de projectscope EN het publiek.** Platformgebeurtenissen dragen geen project (deel 2, punt 4: "gebeurtenissen zonder project zijn platformgebeurtenissen en zijn alleen voor beheerders") en vallen al op de eerste af. Maar de projectscope alleen is niet genoeg, en de kortste weg ("categorie 12 is voor platformbeheerders, dus die gebeurtenissen dragen geen project") klopt niet. Geteld op de projectSCOPE en niet op de kolom `onderwerp` (die zegt volgens deel 1 `:49` waar een gebeurtenis OVER gaat, wat een andere eigenschap is), kent deel 1 er zes die WEL een project dragen en toch publiek `B` en niets anders hebben. Vier bestaan vandaag: de weggegooide in-memory wizardtaak (deel 1 `:141`, categorie 12), de doorgestuurde federatietaak (deel 1 `:301`, categorie 12), de ingediende aanvraag (deel 1 `:175`, categorie 6) en de aan een realm-gebruiker toegekende projectrol (deel 1 `:278`, categorie 8). Twee staan in de catalogus als "bestaat nog niet", allebei in categorie 6 en allebei in dezelfde tabel als `:175`: een aanvraag die al lang openstaat (deel 1 `:178`) en een ingetrokken aanvraag (deel 1 `:179`). Die twee schrijven hun onderwerp als "idem", en dat "idem" verwijst naar de "dienst binnen een project" van `:175`, dus een sweep op het woord "project" ziet ze niet; ze staan hier bij naam zodat de opsomming compleet is. `:278` is op die kolom al helemaal niet te vinden, want zijn onderwerp is "gebruiker"; dat hij toch een project draagt is gemeten op `realm_name` in `opi/manager/invite_manager.py:187` en `:153`, en deel 2, punt 4 rekent de hele telling daar rij voor rij voor. Niet meegeteld zijn de weessweep-rijen `:229` en `:231`, die ook publiek `B` dragen: hun onderwerp is "dienst" en deel 1 `:529` legt ze bij de sweep zelf en niet bij een project. Alle zes gaan deze webhook niet uit, ook niet als de abonnee categorie 12, 8 of 6 aankruist. Voor vijf ervan is dat wat deel 1 `:535` bedoelt met "type 12 is alleen zichtbaar voor platformbeheerders; type 6 alleen voor wie beoordeelt" (`:141` en `:301` in categorie 12, `:175`, `:178` en `:179` in categorie 6), en de grendel die dat waarmaakt is het publiek en niet de scope. De zesde, `:278`, komt in die zin niet voor: hij valt in categorie 8, en die categorie staat verder vol rijen met publiek `A`, dus zij kan er niet bij staan. Hem houdt alleen zijn eigen publiek `B` tegen. Die grendel staat in deel 2, punt 4 en luidt daar: een gebeurtenis komt alleen op de projectkant als hij een project draagt EN zijn publiek niet uitsluitend `B` is. Net als de bewaartermijn hangt hij aan de SOORT en niet aan de categorie, dus de bezorger toetst hem per soort. Dat is dezelfde grens als bij het lezen en geen tweede regel ernaast, en `cross-domain-access` laat zien dat hij ook echt zo werkt: die gebeurtenis wordt twee keer geschreven met een verschillende `subject` (deel 2, punt 4), dus elk van de twee projecten krijgt op zijn eigen webhook alleen zijn eigen exemplaar.
4. **Waar de aanvraag heen mag.** Dit is de regel die het makkelijkst wordt overgeslagen en die het duurst is om over te slaan, want de ontvanger-URL komt van een gebruiker terwijl de aanvraag vertrekt met de netwerkpositie van OPI. Het uitgaande beleid dat hierboven onder "wat er al ligt" staat is `to: []` op TCP/443, dus zonder een eigen eis is elke bestemming die vanuit de OPI-pod op 443 bereikbaar is een geldige ontvanger, ook `https://kubernetes.default.svc`, ook een intern dashboard in een andere namespace, ook een adres in een privebereik aan de andere kant van de egress-gateway. Met de exponentiële herhaling uit het outbox-patroon hierboven erbij is dat geen bezorger meer maar een aanvraaggenerator die een aanvaller op afstand kan richten. **De regel.** Alleen schema `https`, en niets anders. De ontvanger-URL wordt bij het OPSLAAN opgelost, en elk adres waarop de naam uitkomt moet een publiek adres zijn: geen loopback (`127.0.0.0/8`, `::1`), geen link-local en dus ook niet het metadata-adres (`169.254.0.0/16`, `fe80::/10`), geen privebereiken (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `fc00::/7`), geen unspecified of multicast, en geen naam die op `.svc`, `.svc.cluster.local` of `.cluster.local` eindigt. Bij het BEZORGEN wordt opnieuw opgelost en wordt er verbonden met het adres dat op dat moment is gecontroleerd, want tussen opslaan en bezorgen kan dezelfde naam een ander adres opleveren (DNS-rebinding), en een controle die alleen bij het opslaan gebeurt houdt dat niet tegen. Redirects worden niet gevolgd, want een 302 verplaatst de bestemming buiten elke controle om; een 3xx is een bezorgfout en geen doorverwijzing. Plus een vaste, korte time-out per poging. **Dit is nieuw werk en geen bestaand hulpmiddel aanroepen:** er is vandaag niets in `opi/` om dit van te erven. Op de meetbasis staan er zeven aanroepen van `urlparse` in `opi/` en geen ervan controleert een bestemming: `opi/utils/csrf.py:293` en `:303` toetsen de INKOMENDE `Origin` en `Referer`, `opi/middleware/security_headers.py:41` knipt scheme en host uit een URL voor een CSP-regel, `opi/connectors/git.py:212` en `:305` en `opi/utils/naming.py:1244` ontleden een git-URL in onderdelen, en `opi/utils/secrets.py:295` haalt de hostnaam uit de OIDC-basis-URL. De dichtstbijzijnde verwant is de enige `ipaddress.ip_address()` in de codebase, `opi/connectors/postgres.py:392`, en die doet iets anders: hij WEIGERT een letterlijk IP als databasehost en zegt niets over welk adres bereikbaar mag zijn. De implementeur van fase 6 heeft dus alleen deze regel.
5. **Wie een abonnement mag aanmaken, en dat is niet de projectsleutel.** Dit kanaal duwt projectgegevens de deur uit naar een bestemming die een gebruiker kiest, dus de SCHRIJFkant ervan heeft een regel nodig naast de leeskant uit deel 2 punt 4. **Die regel geldt voor de KLASSE en niet alleen voor dit kanaal:** elk kanaal waarvan de bestemming door een gebruiker wordt gekozen valt eronder, en dit plan kent er nog een, de Mattermost-kanaalwebhook uit Kanaal 4. Die is hier niet uitgewerkt, maar hij heeft dezelfde vorm en dus dezelfde twee regels, deze en ontwerpregel 4. **Gekozen: een abonnement aanmaken, wijzigen of intrekken is een handeling van een projectbeheerder met een IDENTITEIT**, dus langs `opi/api/user_token_auth.py` of langs de sessie in de web-UI, met `get_user_role_for_project` (`opi/services/project_authorization.py:60`) in `PROJECT_EDIT_ROLES` (`:27`, vandaag `admin` en `owner`). **Uitdrukkelijk niet op de `X-API-Key` van het project**, ook al mag diezelfde sleutel de projecttijdlijn wel LEZEN (Kanaal 2). De reden staat als commentaar boven die constante en is hier doorslaggevend: die sleutel "opens every mutating per-project API route and carries no role of its own" (`opi/services/project_authorization.py:25`). Hij identificeert een project en geen mens, hij wordt uitgedeeld aan iedereen met een bewerkrol en is dus per definitie gedeeld (`opi/api/v2/router.py:1177` geeft hem aan elke rol in `PROJECT_EDIT_ROLES`, met het commentaar erboven: "the API key grants full project mutation and knows no roles"), en er is er precies een per project, dus hem intrekken breekt elke andere integratie van dat project tegelijk. Een bestemming waar de gebeurtenissen van een heel project naartoe stromen mag niet neergezet kunnen worden door een houder die achteraf niet aan te wijzen is: dat maakt de actor uit BIO2 8.15.01 (deel 2, paragraaf 6) op precies de handeling die hem het hardst nodig heeft leeg. **De prijs, en die is echt:** een agent kan zijn eigen webhook niet aanzetten met de sleutel die hij al heeft, en dat is een handeling erbij voor publiek C. Dat is aanvaard, want het gaat om een instelling die een mens een keer doet en niet om de dagelijkse weg. **En de handeling zelf is een gebeurtenis**, van type 9 `dienstwijziging` uit deel 1 (publiek A en C, dus zichtbaar op de projecttijdlijn en niet alleen voor platformbeheerders zoals type 12 zou zijn), met de ontvanger-URL in `data` en zonder het geheim, zodat er in de tijdlijn van het project staat wie deze bestemming heeft neergezet en wanneer.

### Waarom dit het eerste push-kanaal is

Zie de beslechting bovenaan. Kort: het bedient publiek C, dat vandaag niets heeft; het heeft geen persoonsgegevens nodig in het kanaal zelf; het is per project af te schermen; en het hangt niet af van een keten buiten ons beheer. Het is ook het enige kanaal dat zijn opbrengst uit zichzelf bij de agent AFLEVERT zonder dat daar een persoonlijke identiteit voor nodig is (Kanaal 2 bedient publiek C ook, maar daar moet de agent zelf om vragen); het NEERZETTEN van de bestemming vraagt die identiteit wel, en dat is ontwerpregel 5 hierboven.

---

## Kanaal 6: Alertmanager, de andere helft

Wat een drempelwaarde over een reeks is (wachtrijlengte, foutpercentage, backups die niet liepen, "al N dagen geen geslaagde backup" uit deel 1 paragraaf 5) hoort niet in een gebeurtenissenlog maar in een metriek met een regel eroverheen. Het is geen concurrent van de gebeurtenissen maar de andere helft.

**Wat er nodig is:** Alertmanager uitrollen, alerteringsregels schrijven, en een route naar ntfy of mail. Plus een teller per gebeurtenissoort in `opi/core/metrics.py`, dat vandaag alleen `GaugeMetricFamily` kent en geen enkele `Counter` voor een domeingebeurtenis.

**Wat er al ligt:** Prometheus draait. **Wat er niet ligt:** `infrastructure/bootstrap/infrastructure/prometheus/controller/base/configmap.yaml` heeft alleen `scrape_configs`, geen `rule_files` en geen `alerting`, en er is geen bestand in `infrastructure/` of `bootstrap/` dat het woord alertmanager noemt.

**Waarom dit voor mail komt:** Alertmanager valt binnen ons beheer en de mailketen niet. Dat is dezelfde regel als bij de webhook.

---

## De voorkeuren

### Het scherm

Op `/account/meldingen` (VOORSTEL), naast de bestaande accountpagina. Een tabel: de typen uit deel 1 als rijen, de persoonlijke kanalen als kolommen, een aanvinkvakje per snijpunt. Voor een gewone gebruiker zijn dat er elf van de twaalf: type 12 (`beheer-en-beveiliging`) is alleen voor platformbeheerders en staat niet in zijn scherm.

**Waarom type 6 er wel bij staat.** Deel 1 zegt bij de typentabel dat type 6 (`aanvraag-ingediend`) alleen is voor wie beoordeelt, en de rollentabel hieronder geeft "aanvragen" per mail alleen aan de platformbeheerder. Dat sluit een projectbeheerder niet uit, want wie beoordeelt is een eigenschap van de goedkeuring en niet van de rol: `ApproverScope` kent naast `PLATFORM_ADMIN` ook `PROJECT_ADMIN` en `PROJECT_MEMBER` (`opi/services/catalog/approval.py:45-56`), en `service_use_approval` neemt de beoordelaar als parameter met `PLATFORM_ADMIN` alleen als standaardwaarde (`:170-176`). Vandaag staan alle drie de gedeclareerde goedkeuringen op `PLATFORM_ADMIN` (deel 1, paragraaf 2), dus voor een projectbeheerder blijft de rij in de praktijk leeg tot er een goedkeuring binnen het project komt te liggen. Dat is precies de reden om het vinkje wel te tonen: het scherm gaat over typen en niet over de goedkeuringen die er vandaag toevallig zijn, en een type dat pas verschijnt zodra de eerste projectgebonden goedkeuring wordt gedeclareerd, is een scherm dat onder je handen verandert. De tabel in deel 1 geeft type 6 om dezelfde reden een projectbeheerderstandaard.

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

Boven de tabel: welke rol je hebt en dus welk standaardprofiel je krijgt, met een knop om terug te zetten naar de standaard. Onder de tabel: de koppeling met Mattermost, als dat kanaal er is.

**Elf rijen keer drie kolommen is 33 vakjes.** Dat is veel, en het is de prijs van "per type per kanaal". De rem erop is dat niemand het scherm hoeft te openen: de standaarden per rol kloppen, en wie ze nooit aanraakt krijgt iets bruikbaars.

**De webhook staat hier bewust niet bij**, en dat is dezelfde regel als bij de Mattermost-kanaalwebhook: hij is een PROJECTabonnement en geen persoonlijk kanaal. Hij hoort op de projectpagina en niet op de accountpagina, want anders gaat iemand ervan uit dat zijn persoonlijke instelling ook bepaalt wat de tooling van het project binnenkrijgt.

### De standaarden per rol

Uit de tabel in deel 1, samengevat:

| Rol | Postvak | E-mail | Redenering |
|---|---|---|---|
| **Platformbeheerder** | alles, inclusief type 12 (beheer en beveiliging) | aanvragen, storingen, onomkeerbare ingrepen | hij is de eerstelijns; de rest ziet hij als hij kijkt |
| **Projectbeheerder** (`admin`, `owner`) | alles van zijn projecten | uitrol-mislukkingen, gezondheid, platformingrepen, gegevens, besluiten over zijn aanvragen, ledenwijzigingen | hij is verantwoordelijk voor wat er met het project gebeurt |
| **Projectlid** (`member`, `developer`) | uitrol, gezondheid, verwijderingen, leden, mededelingen | alleen mededelingen van het platform, en wat hij zelf startte | hij werkt eraan mee, hij bestuurt het niet |
| **Actor** (bovenop je rol) | wat je zelf startte | mislukkingen van wat je zelf startte | je eigen handeling is altijd van jou, ook als je verder geen rol hebt |

De tijdlijn valt hier buiten: die kent geen voorkeuren, want je gaat er zelf naartoe kijken.

### "Waarom kreeg ik dit bericht"

Elke melding draagt hem: de kolom `reason` in `notification_deliveries` (`project-admin`, `project-member`, `actor`, `approver`, `platform-admin`, en `platform-announcement` (VOORSTEL) voor de ene soort zonder project die toch bij iedereen wordt neergelegd, deel 2 punt 4). In het postvak staat hij als een regel onder de melding ("Je krijgt dit omdat je beheerder bent van project X", en bij de zesde waarde "Je krijgt dit omdat je gebruiker bent van dit platform"); in de mail staat hij onderaan, naast de link naar het voorkeurenscherm.

**Dit is de kolom die het model verdient.** Zonder hem is de enige eerlijke tekst "je krijgt dit omdat een regel ergens vond dat je het moest hebben", en dat is precies de tekst die mensen alles laat uitzetten.

### Wat niet uitgezet mag kunnen worden

Zo weinig mogelijk, want elke onuitschakelbare melding is er een die mensen leert dat het scherm niet werkt. Het voorstel is **twee gevallen**, en beide alleen voor het postvak (mail mag altijd uit):

1. **Onomkeerbare ingrepen door het platform.** "Een gemarkeerde resource is definitief verwijderd" (deel 1, paragraaf 4). De melding komt achter de daad aan en er is niets meer aan te doen. Dat mag niemand hebben gemist, ook niet door een vinkje.
2. **Je bent uit een project verwijderd, of je rol is gewijzigd.** Het gaat over jouw eigen toegang. Iemand die dit uitzet, weet niet meer waar hij bij mag.

**En expliciet WEL uitzetbaar, ook al is het verleidelijk om anders te kiezen**: mislukte deploys. Een projectlid dat er tien per dag heeft omdat hij aan het uitproberen is, moet ze uit kunnen zetten, anders zet hij het hele systeem uit. De projectbeheerder houdt ze standaard aan.

---

## De fasering

**De regel: elke fase heeft op zichzelf waarde en is apart uit te rollen.** Geen enkele fase is een voorwaarde voor de volgende in de zin dat hij anders niets doet; ze stapelen.

### Beslecht: welke bron de eerste schrijfweg wordt

**De tegenspraak.** Het ene bronstuk begint met goedkeuringen: er zijn er maar drie, ze zijn zeldzaam, de belanghebbende is bijna altijd een platformbeheerder (dus weinig autorisatiewerk), de aanroeppunten zijn twee functies in `opi/services/approvals.py`, en de pijn is echt. Het andere begint met de resource-tuner: dat is de enige bron die vandaag al een compleet gevormde gebeurtenis produceert, hij raakt alle drie de niveaus, hij heeft een niet-menselijke actor, en hij is daarmee de goedkoopste manier om te ontdekken of de kolomkeuze klopt.

**Beslecht: de resource-tuner eerst, goedkeuringen meteen daarna.** Ze beantwoorden verschillende vragen: het ene argument gaat over de SCHRIJFWEG (houdt de recordvorm stand) en het andere over de LEZER (wie heeft er op dag een iets aan). De verkeerde kolomkeuze is de dure fout, want die is later een migratie over de hele tabel, en de resource-tuner is de enige bron waarmee je die keuze kunt toetsen zonder iets te verzinnen. Dat weegt zwaarder dan een fase eerder waarde leveren.

**Wat de verliezende redenering aandroeg en wat overeind blijft:** het criterium. Een fase wordt niet afgerekend op een rij in een tabel maar op een lezer, en daarom eindigt fase 1 met een tijdlijn die iemand kan openslaan en niet met een migratie. Verder blijft de hele onderbouwing staan waarom goedkeuringen de goede TWEEDE bron is, en die is sterker dan voor elke andere kandidaat: weinig volume, weinig autorisatiewerk, twee aanroeppunten, en een wachtende mens.

### Fase 1: de tabel, een bron, en een tijdlijn

**Wat**: de vier ORM-modellen en de migratie (alle vier de tabellen in een keer, zodat de fasen 2 tot en met 5 geen migratie zijn; de twee kanaaltabellen uit deel 2 komen wel als migratie, in fase 6 en fase 9), een schrijfdienst in de lijn van `AsyncTaskService`, de gesloten soortenenum met de eerste waarden erin, de schrijfweg in de resource-tuner, en een tijdlijnblok op de deploymentpagina achter `is_user_authorized_for_project`, met de tweede grendel uit deel 2 punt 4 erbij (alleen soorten waarvan het publiek niet uitsluitend `B` is, en zonder de kolom `origin`).

**Waarde op zichzelf**: de vraag "waarom heeft mijn component ineens meer geheugen" is beantwoordbaar zonder het projectbestand te openen.

**Verifieerbare uitkomst**: zie "de kleinste eerste stap" hierboven.

| Bestand | Wat |
|---|---|
| `opi/services/persistence/notifications.py` | nieuw: de vier ORM-modellen uit deel 2 |
| `opi/services/persistence/__init__.py` | de modellen erbij importeren |
| `opi/migrations/versions/005_add_notifications.py` | nieuw |
| `opi/services/gebeurtenissen.py` | nieuw: schrijven, lezen per project en per deployment, de soortenenum |
| `opi/core/resource_tuning_scheduler.py`, `opi/services/resource_tuning_service.py` | de schrijfweg |
| `opi/core/config.py` | de instellingen (aan/uit, bewaartermijnen) |
| `opi/web/router.py` (`project_deployment_details`, `:1385`) plus `opi/templates_lotc/` | het tijdlijnblok op de deploymentpagina |
| `opi/web/lotc_fixtures.py` | de proefopstelling, met zichtbaar verzonnen waarden |
| `tests/test_gebeurtenissen.py`, `tests/e2e/test_gebeurtenissen.py` | nieuw |
| `features/gebeurtenissen.md` | nieuw |

**Niet doen in deze fase**: geen tweede bron, geen postvak, geen uitwaaiering, geen outbox, geen melding, geen abonnement, geen export, geen retentielus.

### Fase 2: het postvak per persoon, en goedkeuringen als tweede bron

**Wat**: de uitwaaiering, de outbox met zijn planner, het postvak, de teller in de kop, de API voor lezen en markeren, en de tweede bron: goedkeuringen (aanvraag ingediend, goedgekeurd, afgewezen).

**Waarde op zichzelf**: een platformbeheerder ziet aan de teller in de kop dat er een aanvraag op hem wacht. Dat is af, ook als er nooit een derde fase komt.

| Bestand | Wat |
|---|---|
| `opi/services/notifications.py` | nieuw: uitwaaieren, dedup aan de meldkant, lezen, markeren |
| `opi/core/notification_scheduler.py` | nieuw: de outboxplanner, in de vorm van de bestaande planners |
| `opi/server.py` | de planner starten en stoppen in de lifespan, en de routers registreren |
| `opi/services/approvals.py` | de aanroepen bij `ensure_approval_requests` en `apply_approval_verdicts` |
| `opi/api/v2/notifications_router.py` | nieuw: de endpoints |
| `opi/api/v2/models.py` | de antwoordmodellen |
| `opi/web/router_notifications.py` | nieuw: het postvak en het tellerfragment |
| `opi/templates_lotc/base_lotc.html.j2` | de teller in de hulpbalk |
| `opi/templates_lotc/bg/postvak.html.j2` en `opi/templates_lotc/bg/_meldingen-teller.html.j2` (beide namen VOORSTEL) | nieuw: de pagina en het fragment. De map is `bg/` en niet een nieuwe map `notifications/`, om dezelfde reden als bij fase 8 hieronder: elke ingelogde pagina van dit portaal staat daar al, en het fragment volgt de naamvorm met liggend streepje van `opi/templates_lotc/bg/_tasks.html.j2`, dat hierboven ook het patroon voor de verversing levert |
| `opi/web/menu.py` | het postvak in het menu |

**Niet doen in deze fase**: geen e-mail, geen webhook, geen Mattermost (alleen het postvak); geen voorkeurenscherm (de standaarden per rol staan vast in de code; de tabel is er al); geen draadgroepering in de UI (de kolom `thread_key` wordt wel gevuld); geen samenvattingen.

De regel achter dat lijstje: **kolommen die je later niet meer kunt vullen, worden nu gevuld; gedrag dat je later kunt toevoegen, wordt later toegevoegd.** Dat geldt ook voor `dedup_key`, `thread_key`, `actor` en `subject`: die zijn achteraf niet te vullen.

**Een prijs die genoemd hoort.** De gebeurtenissen uit fase 1 hebben geen ontvangerrijen, en die zijn achteraf alleen te maken met het lidmaatschap van NU en niet met dat van toen. Dat is aanvaardbaar omdat het over een handvol tuningregels gaat en omdat ze op de tijdlijn wel zichtbaar zijn, maar het is een echte eenmalige onvolkomenheid en geen detail.

### Fase 3: de bronnen die het duurst zijn om te missen

**Wat**: de schrijfwegen erbij, gekozen uit de zeven duurste uit deel 1 en uit de taakgroepen: het afkeuren van een projectbestand door de schemavalidatie (`opi/core/git_monitor.py:150`), de uitkomst van de nachtelijke reconciliatie inclusief wat er is opgeruimd (`opi/core/reconciliation_scheduler.py`, `opi/jobs/reconciliation.py`), de uitkomst van een backup en van de retentiesweep (`opi/core/backup_scheduler.py`, `opi/core/backup_retention_sweep.py`), het uitschakelen van een component na een image-pull-fout of een OOM-kill (`opi/services/oom_watcher.py`), de vijf taakgroepen op een plek in de takenwerker (`opi/core/task_worker.py`), de slaapstand (`opi/services/catalog/sleep_mode/scheduler.py`), de runs (`opi/services/runs_service.py`), en de ledenwijzigingen via de vergelijking oud-nieuw op de opslagweg van het projectbestand (`opi/web/router_detail_edit.py`).

Plus een projecttijdlijn naast de deploymenttijdlijn, omdat een deel van deze bronnen op projectniveau hangt. En vanaf hier het dedupvenster met de teller op de ontvangerrij, want vanaf hier is het volume echt.

**Waarde op zichzelf**: dit is het punt waarop de tijdlijn de vraag "wat is er met mijn project gebeurd terwijl ik weg was" beantwoordt. Vanaf hier is er ook voor het eerst een verslag van wat de reconciliatie heeft weggegooid, wat vandaag na een logregel verdwijnt.

**Verifieerbare uitkomst**: een projectbestand met een moedwillige schemafout wordt afgekeurd en levert een gebeurtenis op de projecttijdlijn; een reconciliatieronde met een gemarkeerde resource laat na afloop een regel achter die zegt wat er is opgeruimd, ook nadat de rij in `marked_for_deletion` weg is.

**Niet doen**: de "bestaat nog niet"-regels uit de inventarisatie waarvoor eerst iets waargenomen moet worden (gezond-naar-ongezond-overgangen, "al N dagen geen backup"). Die vragen toestandsgeheugen of een metriek en dat is een eigen stuk.

### Fase 4: wie deed het

**Wat**: de actor doorgeven op de wegen die er een dragen, en waar hij niet bestaat opschrijven dat hij niet bestaat. **De menselijke helft draagt hem al**: `opi/core/task_helpers.py:61` zet `created_by` op het e-mailadres van de sessie, en dat geldt ook voor een agent met een SSO-bearer-token, want die landt in dezelfde vorm in `request.state.user` (`opi/api/user_token_auth.py:260`) en `get_current_user` leest precies dat (`opi/core/auth_decorators.py:57`). **De agenthelft op de projectsleutel heeft niets door te geven**, en dat is geen gat dat deze fase dicht: `:62-63` zet daar de tekst `"API"` in `created_by`, en die reist niet mee naar `actor`, want de sleutel identificeert het project en niet de handelende partij (deel 1 `:467`). Die gebeurtenissen dragen `actor_kind = agent` met `actor` op `NULL` en met hun `origin` erbij; deel 2, punt 7 legt uit waarom een plaatshouder-tekst daar juist schadelijk is. Wat deze fase dus wel aanlegt: de directe bewerkingswegen in `opi/web/router_detail_edit.py` dragen de actor niet; de drie ledenwijzigingen die fase 3 heeft aangelegd krijgen hier dus hun actor. Plus de schrijfwegen die volgens het criterium uit deel 2 punt 6 een recht, een account of een toegangspoging betreffen en daarom de langere bewaartermijn dragen. Dat zijn er in deze fase twaalf, in drie groepen: de toegangspogingen uit deel 1, paragraaf 7 (een geweigerde allowlist-controle op `opi/middleware/authorization.py:117`, een geweigerde API-sleutel in `opi/api/endpoint_util.py`, een geweigerd bearer-token op `opi/api/user_token_auth.py:252`) en de geweigerde inwisselingen uit paragraaf 6 (`opi/manager/invite_manager.py:73`, `:101` en `:128`); de accountwegen (`opi/services/user_admin_service.py:41`, `:51` en `:65`); en de rechtenwegen rond uitnodigingen (`opi/api/invite_routes.py` voor het aanmaken, `opi/manager/invite_manager.py:262` en `:355` voor de inwisseling, `:187` en `:153` voor de projectrol). Elk met het `origin`-veld uit BIO2 8.15.01 erbij. **Samen met de drie ledenwijzigingen zijn dat vijftien van de zestien soorten waarop de langere bewaartermijn staat**; hun namen staan in deel 2 als gesloten lijst, en fase 5 heeft die lijst nodig om meetbaar te zijn. De zestiende (`cross-domain-access`, deel 1 `:379`) hangt aan de dienstwijzigingen en heeft geen eigen fase; wie hem aanlegt, legt hem met actor en oorsprong aan. **En `origin` is niet zomaar `request.client.host`.** Deel 2 punt 7 (`:582`) zet daar de regel neer: `origin` is het adres van de peer van de verbinding, een `X-Forwarded-For` telt alleen als die peer de bekende ingress is en dan de hop die die proxy zelf toevoegde, en op het rechtstreekse in-clusterpad is het het pod-adres van de aanroeper. Op de meetbasis levert `request.client.host` juist wel de door de aanroeper gekozen waarde, want `opi/server.py:590` zet `ProxyHeadersMiddleware(trusted_hosts=["*"])` en die overschrijft `scope["client"]` met de meest linkse `X-Forwarded-For`. Deze fase kan dat dus niet binnen de gebeurtenissencode alleen repareren: het versmallen van die `trusted_hosts` tot de ingress hoort erbij en is een wijziging aan de opzet van de server, met een eigen toets, want ook de scheme-afleiding uit `X-Forwarded-Proto` en elke route die `request.client` leest verandert mee. En `IPRateLimiter.get_client_ip` (`opi/api/router.py:140`) is niet de hulpfunctie om hier te hergebruiken, om de reden die haar eigen docstring geeft (`:146-147`, "X-Forwarded-For can be spoofed"): zij neemt dezelfde meest linkse waarde en is voor snelheidsbegrenzing bedoeld en niet voor bewijs. Dat weegt in deze fase zwaarder dan elders, want drie van de twaalf soorten hierboven (de geweigerde allowlist-controle, de geweigerde API-sleutel en het geweigerde bearer-token) worden juist op een NIET-geauthenticeerd verzoek geschreven, en het inkomende netwerkbeleid laat elke pod in het cluster rechtstreeks bij poort 8000 (`bootstrap/rig-system/kustomize/operations-manager/overlays/odcn-production/network-policy.yaml:12-16`).

**Waarde op zichzelf**: dit is de fase die de compenserende maatregel uit `features/bio-network-access-no-vpn-compliance.md` waarmaakt en die bevinding E uit `plans/technische-review-bio-en-nora-bevindingen.md` dicht: een verslag met een actor, dat langer bestaat dan een uur. Het is ook de fase die de openstaande regel uit de post-mortem-tijdlijn ("Controle toegang tot Wies/ZAD/Keycloak wijzigingen") in de toekomst beantwoordbaar maakt.

**Verifieerbare uitkomst**: een lid toevoegen via de portal levert een gebeurtenis met het e-mailadres van de handelende beheerder EN met de netwerklocatie waarvandaan hij het deed; een account aanmaken en meteen weer verwijderen levert twee gebeurtenissen die allebei die beheerder aanwijzen; drie keer een verkeerde API-sleutel aanbieden levert drie beveiligingsgebeurtenissen op de platformtijdlijn en geen enkele op een projecttijdlijn; en de agenthelft is even hard te toetsen, maar met TWEE VERSCHILLENDE handelingen, want het criterium is de WEG en niet de handeling. Er bestaat namelijk geen handeling die langs allebei kan: de twee verzamelingen routes zijn disjunct. Vijfenvijftig routes dragen `@validate_api_token`, en die weigert zonder `X-API-Key` en heeft geen terugval op een token (`opi/api/endpoint_util.py:50-53`); precies twee dragen `@validate_user_token`, namelijk `list_projects_v2` (`opi/api/v2/router.py:1126`), die geen taak aanmaakt, en `create_project_v2` (`:1199`), die per definitie niet op de projectsleutel kan omdat het project dan nog niet bestaat (zijn eigen docstring, `:1205-1209`). Ook de drie handelingen hierboven kunnen geen van drieen op de sleutel: leden lopen via `opi/web/router_detail_edit.py` en er is geen `/projects/{name}/users` in de v2-router, accountbeheer heeft precies een aanroeper (`opi/web/router_user_admin.py:17`), en een geweigerde sleutel start geen taak. De toets is dus tweeledig. Een component toevoegen op de `X-API-Key` van een project (`add_component_v2`, `opi/api/v2/router.py:1566`, achter `@validate_api_token` op `:1565`, taak op `:1600`) levert een gebeurtenis met `actor_kind = agent`, met de netwerklocatie in `origin` en met `actor` leeg, dus zonder de tekst `API` erin. Een project aanmaken met een bearer-token (`create_project_v2`, `opi/api/v2/router.py:1199`, de enige bearer-route die een taak start) levert er een met datzelfde `actor_kind = agent` maar met het geverifieerde e-mailadres uit het token in `actor`. Samen pinnen die twee het criterium van beide kanten. **En de oorsprong is apart te toetsen, want dat is de helft die het stilst verkeerd gaat.** Een verzoek dat rechtstreeks aan de pod wordt aangeboden, buiten de ingress om en met een verzonnen `X-Forwarded-For`, levert een gebeurtenis met het WERKELIJKE peer-adres in `origin` en niet met de verzonnen waarde; hetzelfde verzoek via de ingress levert het adres dat de ingress zag. De toets die op de meetbasis zou FALEN is de eerste, en dat is precies de reden dat de regel uit deel 2 punt 7 (`:582`) bij deze fase hoort.

**Let op bij deze fase.** Vanaf hier staan er persoonsgegevens in de tabel, en dus is de retentielus uit fase 5 geen luxe meer maar een voorwaarde. Ze kunnen ook samen worden uitgerold; dan zijn fase 4 en 5 een stap.

### Fase 5: retentie en redactie

**Wat**: een tweede lus in de outboxplanner, in de vorm van `cleanup_old_tasks` (`opi/core/async_task_service.py:670`), die na 90 dagen `actor` en `origin` pseudonimiseert in plaats van de rij te verwijderen (allebei op `NULL` en niet op een plaatshouder-tekst, want anders krimpt de partiele index nooit; deel 2, punt 6), de rij na een jaar opruimt, en daarbij de gesloten lijst van zestien soorten uit deel 2 punt 6 overslaat, dus alles wat een recht, een account of een toegangspoging betreft: een `type <> ALL (...)` in dezelfde `WHERE`, op de soort en niet op de categorie, want die lijst loopt dwars door de typen 8 en 12 heen. Plus de redactiefunctie op de schrijfweg, zodat een connectorfout nooit ongefilterd in `data` belandt, en de opruiming die meeloopt met `UserAdminService.delete_user`.

**Waarde op zichzelf**: de verwerking is begrensd en uitlegbaar, en de tabel groeit niet onbeperkt.

**Verifieerbare uitkomst**: een gebeurtenis ouder dan de termijn draagt in `actor` en in `origin` allebei `NULL` maar staat er verder nog steeds, en valt daarmee uit `idx_notification_events_actor_sweep` (te meten door de pseudonimiseringspas een tweede keer te draaien: die raakt de rij niet meer aan); dat geldt ook voor een gebeurtenis van categorie 12 die niet op de lijst staat (bijvoorbeeld "een subdomein is geclaimd", deel 1 `:302`); een gebeurtenis van een soort die er wel op staat is op dezelfde leeftijd nog volledig, actor en oorsprong incluis, en dat is te meten aan de twee kanten van het criterium tegelijk: een geweigerde sleutel (`nl.rig.zad.beveiliging.sleutel-geweigerd.v1`, categorie 12) en een toegekende projectrol (`nl.rig.zad.beveiliging.projectrol-toegekend.v1`, categorie 8) overleven allebei; het account van die beheerder laten opheffen wist zijn actor uit de gewone gebeurtenissen en laat hem op die twee staan; een testgebeurtenis met een wachtwoord in het foutveld komt geredigeerd in de database terecht en niet pas geredigeerd op het scherm.

### Fase 6: de webhook per project

**Wat**: een abonnementstabel (`notification_subscriptions`, VOORSTEL), een geheim per abonnement met een HMAC-handtekening, het watermerk uit deel 2 punt 3 zodat een herstart geen dubbele of gemiste bezorging oplevert, de plek van het aan-uit-vinkje volgens de regel uit Kanaal 5, en de twee VEILIGHEIDSgrendels uit ontwerpregel 4 en 5 van dat kanaal: de bestemmingscontrole op de ontvanger-URL en de eis dat alleen een projectbeheerder met identiteit een abonnement neerzet. Die twee heten hier zo om ze uit elkaar te houden van de twee BEREIKgrendels uit ontwerpregel 3, de projectscope en het publiek. De veiligheidsgrendels zijn geen afronding achteraf maar horen in dezelfde fase als de bezorger, want een bezorger zonder bestemmingscontrole is bruikbaarder voor een aanvaller dan voor het project.

**Waarde op zichzelf**: de agent hoeft niet meer te pollen op een taakstatus die na een uur verdwijnt.

**Verifieerbare uitkomst**: een testontvanger krijgt precies een bezorging voor een gebeurtenis waarop hij is geabonneerd, met content-type `application/json` en niet `application/cloudevents+json` zolang `source` een plaatshouder draagt (de uitzondering op regel 1 uit deel 2, zie Kanaal 5 ontwerpregel 3); een ontvanger die 500 teruggeeft krijgt herhalingen met oplopende tussenpozen en geen oneindige lus; een OPI-herstart midden in een venster levert geen tweede bezorging van al bezorgde gebeurtenissen. En de twee veiligheidsgrendels, in dezelfde vorm als de waarschuwing bij het inkomende endpoint (Kanaal 2): een abonnement met een cluster-interne, loopback-, link-local- of prive-ontvanger-URL wordt al bij het OPSLAAN geweigerd en er gaat geen enkele aanvraag uit, wat te meten is door mee te kijken op de uitgaande verbindingen van de pod; een ontvanger die met een 302 naar zo'n adres wijst krijgt geen tweede aanvraag; en een abonnement aanmaken lukt met een projectbeheerderidentiteit en mislukt met de `X-API-Key` van datzelfde project, terwijl de projecttijdlijn op die sleutel wel gewoon leesbaar blijft. En de twee bereikgrendels uit ontwerpregel 3, want die zijn met een test te pinnen en anders sluipen ze eruit: een abonnement van project A dat alle categorieen aankruist, categorie 12 incluis, krijgt geen enkele bezorging van een platformgebeurtenis en geen enkele van een gebeurtenis van project B, en ook geen enkele van een gebeurtenis die wel project A draagt maar publiek uitsluitend `B` heeft. Dat zijn de zes rijen uit ontwerpregel 3, en de toets kan er vandaag vier van opvoeren omdat de andere twee nog niet bestaan: `:141`, `:175`, `:278` en `:301` zijn te schrijven, `:178` en `:179` (de twee "bestaat nog niet"-aanvraagrijen) horen bij dezelfde toets zodra hun soort er is. En geen enkele bezorgde body draagt een `origin`-veld.

### Fase 7: Alertmanager en de metriekhelft

**Wat**: Alertmanager uitrollen, alerteringsregels op Prometheus, een route naar ntfy, en een teller per gebeurtenissoort in `opi/core/metrics.py`.

**Waarde op zichzelf**: de dingen die een tijdlijn niet kan zeggen ("de wachtrij loopt op", "drie backups op rij gemist", "al N dagen geen geslaagde backup") krijgen het gereedschap dat daarvoor bedoeld is.

**Verifieerbare uitkomst**: een alerteringsregel op een kunstmatig opgevoerde teller bereikt ntfy.

### Fase 8: de voorkeuren en het e-mailkanaal

**Wat**: het voorkeurenscherm, de standaardprofielen per rol, de SMTP-connector, de samenvatting, de afmeldlink, en de weergavenaam op het platformaccount.

**Bestanden**: `opi/connectors/mail_sender.py` (nieuw, of erbij in `opi/connectors/mail.py`), `opi/web/router.py:2608` (daar zit `/account` vandaag; het voorkeurenscherm hoort ernaast), `opi/templates_lotc/bg/meldingen.html.j2` (nieuw; de map is `bg/` en niet `account/`, want `/account` rendert vandaag `opi/templates_lotc/bg/account.html.j2` (`opi/web/router.py:2622`) en een map `opi/templates_lotc/account/` bestaat niet), `opi/api/v2/notifications_router.py` (de twee voorkeurendpoints).

**Voorwaarde die buiten dit traject ligt**: de relay moet op productie aanstaan (`plans/mail-vervolgpunten.md` punt 5), en er moet een antwoord zijn op de vraag wat er gebeurt met een ontvanger buiten `rijksoverheid.nl` (punt 8) en met een bounce (punt 10). Dat zijn voorwaarden en geen context.

**Verifieerbare uitkomst**: een testabonnement levert precies een mail per venster met de gebeurtenissen gegroepeerd, de afmeldlink werkt zonder dat er een taak of een commit ontstaat, en een mislukte bezorging levert een zichtbare gebeurtenis op in plaats van stilte.

### Fase 9: Mattermost, de export, het inkomende endpoint en de platformmededeling

**Wat**: vier dingen, waarvan de eerste drie los van elkaar staan en het vierde de keerzijde van het derde is. De bot met de koppeling via verificatiecode. De CloudEvents-projectie op de rand, met de `source` uit de OIN-beslissing. Het inkomende endpoint waarop een systeem van buiten een gebeurtenis kan aanbieden, met de zes ontwerpregels uit Kanaal 2, waarvan de laatste drie (scope en `source` uit de autorisatie, servergezette actor en tijd, en geen soort met een te breed publiek) de veiligheidsgrendels van deze route zijn, precies zoals ontwerpregel 4 en 5 dat bij Kanaal 5 zijn. En de route voor de platformmededeling (`POST /api/v2/platform-announcements`, VOORSTEL), waarmee een platformbeheerder met identiteit de soorten van type 11 neerzet die ontwerpregel 6 juist van buiten weert: onderhoud (deel 1 `:305`), een nieuwe release (deel 1 `:304`) en de overige clusterbrede berichten. Die route hoort in deze fase en niet eerder, want zij bestaat alleen dankzij ontwerpregel 6, en het mailkanaal dat type 11 als standaardkanaal draagt ligt er sinds fase 8. **Bij die route hoort een stuk werk dat niet in de route zelf zit en daarom makkelijk wordt vergeten**: de uitwaaiering uit fase 2 kent alleen het lidmaatschap van een project, en deel 2 punt 4 geeft type 11 daarnaast een tweede ontvangerregel, namelijk het platformregister (`opi/services/user_admin_service.py:23`, `list_users`), met `reason = platform-announcement` op de ontvangerrij. Zonder die tweede regel in `opi/services/notifications.py` schrijft de route wel een gebeurtenis en krijgt niemand hem, en dan zijn deel 1 `:523` en het rolprofiel van het projectlid hierboven niet gehaald.

**Voorwaarde voor het eerste**: het antwoord op "welke Mattermost en waar staat hij". **Voorwaarde voor het tweede**: de OIN.

**Verifieerbare uitkomst**: een uitgaande projectie valideert tegen het NL GOV profiel op de vier verplichte attributen, en een projectie van een gebeurtenis met een plaatshouder-`source` wordt geweigerd in plaats van verstuurd. En voor het inkomende endpoint, in dezelfde vorm als de veiligheidsgrendels van fase 6: een POST met de `X-API-Key` van project A die in de body een `subject`, `project` of `source` van project B zet, landt op project A en niet op project B, de aangeboden `source` komt in geen enkele kolom terecht, en de tijdlijn en de webhook van project B blijven leeg; dezelfde POST met een `type` waarvan het publiek buiten project A reikt wordt geweigerd met een 403 en er wordt niets weggeschreven, wat te meten is door de rijen in `notification_events` te tellen voor en na; een POST die `actor`, `actor_kind`, `time` of `origin` in de body meestuurt levert een rij op met de door de server gezette waarden (`actor_kind = external`, `time` de ontvangsttijd, `actor` leeg op een sleutelweg) en niet met die uit de body; een anonieme POST wordt geweigerd; en `onderhoud gepland` (deel 1 `:305`) lukt op dit endpoint op geen enkele sleutel, ook niet op de `ADMIN_API_KEY`, en evenmin op een bearer-token, want deze route neemt die soort helemaal niet aan; hij lukt wel op `POST /api/v2/platform-announcements` langs de sessie of een bearer-token van een platformbeheerder, waarbij die rij dan een actor draagt en `actor_kind = human`, en diezelfde aanroep zonder platformbeheerdersrol levert een 403 en geen rij op. Dat laatste geldt voor elke soort van type 11 en niet alleen voor onderhoud. **En de bezorging wordt in dezelfde toets meegemeten**, want deze route bestaat om de mededeling bij iedereen te krijgen en niet om een rij in `notification_events` te zetten: na een geslaagde mededeling staat er een rij in `notification_deliveries` voor een gebruiker die nergens beheerder is en die geen lid is van enig project dat met de mededeling te maken heeft, met `reason = platform-announcement` erop (deel 2 punt 4 en punt 7), en die rij staat in zijn postvak en telt mee in zijn ongelezenteller. Een toets die alleen de gebeurtenisrij telt is dus niet genoeg: zonder de ontvangerrij is de mededeling geschreven en niet bezorgd.

---

## Wat we bewust NIET doen, in het hele traject

| Niet | Waarom |
|---|---|
| Een uitgaand abonnement naar derden | geen afnemer; de Abonneren-standaard is nog een werkversie (v0.0.1, niet vastgesteld); het CloudEvents-record houdt het open. Dit is iets anders dan de webhook per project uit Kanaal 5; zie de beslechting bovenaan |
| De logbewaker vervangen | ander publiek (ops), andere bron (Loki), ander kanaal (ntfy); zie deel 1 paragraaf 9 |
| ntfy uitbreiden naar gebruikers | het vraagt een app en een topic, en het heeft geen autorisatie per project |
| Een audittabel als eigen doel bouwen | de gebeurtenissentabel is zo gebouwd dat hij het kan worden; het zelf tot doel maken is een eigen opdracht |
| Meldingen over applicatiegedrag van de klant | dat is van het project, niet van het platform |
| Metrieken en drempelwaarden in de gebeurtenissenlog | Prometheus doet dat, met Alertmanager als de ontbrekende helft; zie Kanaal 6 |
| Meldingen op basis van de beveiligingsscan, image-veroudering of certificaatverval | die gebeurtenissen bestaan niet in OPI; fase 9 maakt ze mogelijk via het inkomende endpoint |
| De websocket-router uitbreiden voor meldingen | per werker geboekhoud, en een permanente verbinding per tabblad is een andere orde dan een logvenster |
| Een `withdrawn`-status voor aanvragen | dat is een uitbreiding van de goedkeuringsmachine, niet van dit traject |
| Meldingen per taaksoort of per dienst instelbaar maken | 23 keer 23 knoppen; de verfijning zit in de gebeurtenis, niet in het scherm |
| `ActionEvent` of `UIEvent` hernoemen | raakt de hele dienstencatalogus, de registry en de dispatch, en levert een woord op; zie deel 2 |
| Dedupliceren bij het vastleggen | dan is achteraf niet meer vast te stellen hoe vaak iets gebeurde, en het botst met de onveranderlijkheid; zie deel 2 punt 3 |
| Vijf ernstniveaus | niemand kan het verschil tussen twee middenniveaus uitleggen, waarna alles naar boven glijdt |

---

## De openstaande beslissingen

Elk punt is met ja of nee te beantwoorden, of het is een vraag aan iemand buiten de code. De aanbeveling staat erbij.

**1. Richting C (postvak per persoon), met de gebeurtenissentabel van B eronder en de CloudEvents-vorm van D.**
*Aanbeveling: ja.* A geeft niet wat de wens vraagt; B alleen kan de melding "je bent uit dit project verwijderd" principieel niet bezorgen en laat de geschiedenis meebewegen met het lidmaatschap. Zie deel 2.

**2. Twaalf meldingstypen, en per type per kanaal een knop (in plaats van een knop per type).**
*Aanbeveling: ja voor allebei*, met werkbare standaarden per rol zodat niemand het scherm hoeft te openen. Het getal twaalf is bespreekbaar; de regel eronder (een type per beslissing die een redelijk mens anders zou nemen) is dat minder.

**3. Fase 1 is de resource-tuner plus een tijdlijn; goedkeuringen zijn fase 2.**
*Aanbeveling: ja.* Zie de beslechting hierboven. Het alternatief (beginnen met goedkeuringen) levert een fase eerder waarde maar toetst de recordvorm niet, en de recordvorm is de dure fout.

**4. De gebeurtenissen ontstaan met losse aanroepen op de plek waar ze gebeuren, plus een vergelijking oud-nieuw op de opslagweg van het projectbestand.**
*Aanbeveling: ja.* Het bestaande hakensysteem (`ActionEvent`) heeft twee leden en is een uitbreidingspunt in de UITROLcyclus; bijna geen enkele gebeurtenis uit de inventarisatie past daarop, en het commit-contract van die familie botst met de outbox. Uitbreiden kan, maar dat is een verbouwing van dat systeem en geen gebruik ervan.

**5. Een eigen outboxplanner in de lifespan van `server.py`, niet de bestaande takenwerker.**
*Aanbeveling: ja.* De takenwerker verwerkt een zware taak tegelijk; een melding zou achter een uitrol in de wachtrij komen. Een eigen planner is de vorm die er al zeven keer staat.

**6. De bewaartermijnen: de actor en de oorsprong gepseudonimiseerd na 90 dagen, de gebeurtenis weg na een jaar, gelezen meldingen 90 dagen, ongelezen meldingen zolang de gebeurtenis bestaat, afleveringen 30 dagen.**
*Aanbeveling: ja.* Merk op dat dit veel langer is dan wat er nu voor taken geldt (een uur), en dat is de bedoeling: dat uur is precies het probleem dat dit oplost.

**7. Hoe lang beveiligingsgebeurtenissen bewaard worden.**
*Aanbeveling: langer dan 90 dagen, en het getal is niet vanuit de code te bepalen.* BIO2 8.15.04 vraagt een risicogerichte termijn met langdurig aanwezige aanvallers in gedachten. Dit is een gesprek met wie verantwoordelijk is voor de risicoafweging in `features/bio-network-access-no-vpn-compliance.md`. Dit is het enige punt in deze lijst dat de bouwer echt blokkeert zodra fase 4 landt. Wat hier NIET meer open staat is welke gebeurtenissen het betreft: deel 2 punt 6 geeft daarvoor een criterium (een recht, een account of een toegangspoging) en de gesloten lijst van zestien soorten die er vandaag onder valt, uitdrukkelijk niet de hele categorie 12 en ook niet alleen de geweigerde pogingen.

**8. Het platformaccount op de relay krijgt een weergavenaam ("ZAD"), en geen eigen plusdeel-adres.**
*Aanbeveling: ja.* De machinerie bestaat (`set_sender_name`), het adres blijft het kale `noreply-rijksapp@rijksoverheid.nl`, en de conventie "plusdeel = project" blijft intact.

**9. De volgorde van de kanalen: tijdlijn, postvak, webhook, Alertmanager, mail, Mattermost.**
*Aanbeveling: ja.* Zie de beslechting bovenaan. De ordenende regel is "van in ons beheer naar afhankelijk van een keten die aantoonbaar nog niet af is".

**10. Mattermost: een bot met privéberichten, met zelfkoppeling via een verificatiecode. Een kanaalwebhook is iets anders en komt later, als PROJECTinstelling.**
*Aanbeveling: ja*, met twee voorbehouden. Eerst moet vaststaan welke Mattermost het is en of hij op het internet staat. Staat hij achter het Rijksnetwerk, dan kan OPI er niet bij en is er een tussenstation in een eigen namespace nodig, precies zoals bij de mailrelay. En de kanaalwebhook valt, zodra hij gebouwd wordt, onder ontwerpregel 4 en 5 van Kanaal 5, want ook daar kiest een gebruiker de bestemming; dat hij als catalogusdienst landt regelt dat niet, want de dienstconfiguratie van een project is met de `X-API-Key` te schrijven.

**11. Een INKOMEND endpoint voor gebeurtenissen van buiten (fase 9), en geen uitgaand abonnement naar derden.**
*Aanbeveling: ja.* Het inkomende endpoint maakt drie "bestaat nog niet"-regels uit de inventarisatie haalbaar voor de prijs van een route, en dat zijn de drie die deel 1 `:312` zelf aanwijst: de beveiligingsscan, de verouderde image en het verlopende certificaat (deel 1 `:306` tot en met `:308`). Alle drie dragen publiek `B` en komen dus alle drie langs de route zelf binnen. Wat er niet langs komt is het type `platform-mededeling` (deel 1 `:523`), waaronder "onderhoud is gepland" (deel 1 `:305`) en "er is een nieuwe release van het platform" (deel 1 `:304`), want hun publiek reikt tot elke gebruiker; die soorten krijgen in dezelfde fase een eigen route voor een platformbeheerder met identiteit. Zie ontwerpregel 6 bij Kanaal 2. Het uitgaande abonnement heeft geen afnemer en de standaard ervoor is nog niet vastgesteld.

**12. De `source`-URN voor CloudEvents vraagt een OIN, en dat is een organisatievraag.**
*Aanbeveling: nu een vaste vorm met een plaatshouder vastleggen, zodat het later een keer invullen is, en de exporteur een plaatshouder laten weigeren.* Zoek dit uit voor fase 1 en niet voor fase 9: de OIN zelf is pas nodig bij de export, maar het antwoord bepaalt of de soortnamen in fase 1 in reverse-DNS-notatie moeten staan. Zo niet, dan is die notatie onnodige omslachtigheid; zo wel, dan is hem later invoeren een migratie over de hele tabel. Dit is het enige punt in deze lijst waar de bouwer niets kan beslissen.

**13. Twee dingen mogen niet uitgezet worden in het postvak: onomkeerbare ingrepen door het platform, en wijzigingen aan je eigen toegang.**
*Aanbeveling: ja, en niet meer dan die twee.* Elke onuitschakelbare melding erbij leert mensen dat het voorkeurenscherm niet werkt.

**14. Of de gezondheidsovergangen van een deployment gebeurtenissen worden.**
*Aanbeveling: ja, maar niet voor fase 5.* Dit is het enige dat "sinds wanneer is dit rood" echt beantwoordt, en het is tegelijk de grootste bron van ruis, want de berekening draait bij elk paginabezoek. Het vraagt een bewaarde vorige stand die er vandaag niet is, en het vraagt de drempels uit deel 2 punt 3 op hun plek. Het is de duurste beslissing in deze lijst en hoort daarom achteraan.

**15. Autogenerate of een SQL-constante voor de migratie.**
*Aanbeveling: autogenerate*, met de gegenereerde migratie nagelezen op de partiele indexen. Zie deel 2, punt 7.

### Beslissingen die inmiddels genomen zijn

Twee vragen die in de bronstukken nog openstonden zijn geen vraag meer, en dat hoort erbij te staan zodat niemand ze opnieuw stelt.

- **Op welke tak dit werk landt.** De vraag was of dit op `main` of op de ontwikkellijn moest, omdat `main` toen 1658 commits achterliep en drie van de verwezen documenten daar niet bestonden. Deze drie documenten staan op de ontwikkellijn en de verwijzingen kloppen daar; `83ac4b9b` is er een voorouder van.
- **Hoe "gebeurtenis" in code gaat heten.** Beslecht in deel 2: het domeinbegrip en de klasse heten `Gebeurtenis`, de tabel- en kolomnamen volgen de bestaande Engelse huislijn, en waar het NL GOV-profiel een naam voorschrijft wint die naam.
