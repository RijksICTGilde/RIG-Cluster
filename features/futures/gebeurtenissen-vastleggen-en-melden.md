# Gebeurtenissen vastleggen en melden: de oplossingsrichtingen

## Status en meetbasis

Dit is deel 2 van drie. Deel 1 is `features/futures/gebeurtenissen-inventarisatie.md` en is het feitenmateriaal waar dit stuk op leunt; deel 3 is `features/futures/gebeurtenissen-plan-van-aanpak.md` en kiest. Dit document weegt af en beveelt aan, maar besluit niet: een lezer die het oneens is met een aanbeveling moet de inventaris nog kunnen gebruiken.

Dezelfde meetbasis als deel 1: commit `83ac4b9b` van 21 augustus 2026. Alle namen die hieronder voor tabellen, kolommen, gebeurtenissoorten, instellingen of endpoints worden gebruikt zijn **VOORSTEL** en als zodanig gemarkeerd. Ze staan er om over te kunnen praten, niet omdat ze zijn besloten.

**Herkomst.** Dit document is de samenvoeging van het inmiddels verwijderde `plans/meldingen-oplossingsrichtingen.md` (22 augustus 2026) en de eerdere versie van dit bestand (21 augustus 2026), gedaan in RC-163 op 28 augustus 2026. De eerste leverde de vier richtingen op de zes assen, het datamodel en de vijf uitgewerkte onderdelen; de tweede leverde de begripsbotsing, de vier extra afgevallen richtingen, de autorisatie en de bewaartermijn met de AVG- en BIO-kant. Waar de twee elkaar tegenspraken staat de beslechting in de tekst, met de reden en met wat de verliezende redenering aandroeg dat overeind blijft. Er zijn vier zulke plekken: de naamgeving in het schema (hieronder), CloudEvents als intern formaat (na de aanbeveling), de Nederlandse naam van het middelste ernstniveau (punt 3, onder Ernst), en waar er wordt ontdubbeld (punt 3). De bewaartermijn is een vijfde; die is in punt 6 beslecht, in twee helften: de twee klokken op dezelfde rij, en de kolom waarop de langere klok zijn uitzondering maakt.

---

## De begripsbotsing, en het besluit

Het woord *event* betekent in deze codebase vandaag drie dingen.

1. **`ActionEvent` en `UIEvent`** (`opi/services/services_enums.py:149` en `:184`, beschreven in `features/service-event-hooks.md`). Twee families van in-procesuitbreidingspunten waarop een dienst inhaakt: `AFTER_SYNC` en `REDEPLOY` schrijven, `PROJECT_SECTIONS`, `DEPLOYMENT_SECTIONS` en `DEPLOYMENT_STATE` lezen. Dit is een dispatchmechanisme met een eigen, uitgeschreven contract (een actiehandler commit nooit zelf; een UI-handler is synchroon en muteert niets). Het is bewust zo ontworpen en het heeft niets met geschiedenis te maken.
2. **De `events`-kolom op `async_tasks`** (`opi/core/async_task_schema.py`). Kubernetes-events die bij een taak zijn opgehaald met `kubectl get events` (`opi/connectors/kubectl.py:969`) en in JSONB zijn geplakt. Dat zijn andermans events, van een namespace, op een moment.
3. **Wat deze opdracht bedoelt:** er is iets gebeurd op het platform dat het waard is te bewaren en mogelijk te melden.

**Besluit: het derde heet een *gebeurtenis*, in code `Gebeurtenis` en in het Nederlands.** De eerste twee blijven ongemoeid. Er wordt niets hernoemd, geen enum verplaatst, geen kolom omgedoopt.

De reden om niet het omgekeerde te doen, dus het derde "event" noemen en de eerste twee hernoemen, is dat de kosten scheef liggen. Hernoemen van `ActionEvent`/`UIEvent` raakt de hele dienstencatalogus (`opi/services/catalog/`), de registry, de dispatch en `features/service-event-hooks.md`, en levert niets op behalve een woord. De `events`-kolom hernoemen kost een migratie. Een nieuw, ongebruikt woord kiezen kost niets en is bovendien preciezer: "gebeurtenis" zegt in het Nederlands wat het is, en dit is een Nederlandstalig project.

Er is een prijs, en die hoort genoemd: naar buiten toe is *event* het woord dat de standaarden gebruiken. Een CloudEvents-projectie op de rand heet dan een event terwijl hij intern een gebeurtenis is. Dat is een vertaalslag op precies een plek (de exporteur) en dat is een aanvaardbare prijs voor nul aanraking van bestaande code.

**Wat er met de eerste twee gebeurt:** niets, met een toevoeging die in deel 3 als latere fase staat. `ActionEvent` is de natuurlijke plek waar een dienst kan besluiten een gebeurtenis te schrijven, maar dat maakt de families geen geschiedenis; het maakt ze een van de bronnen. De `events`-kolom blijft wat hij is en wordt geen bron: hij bevat Kubernetes-events, niet onze eigen.

### Beslecht: wat dit betekent voor de namen in het schema

De twee bronstukken spraken elkaar hier tegen, en de tegenspraak is echt. Het ene besluit hierboven zegt "in code `Gebeurtenis` en in het Nederlands", en stelde Nederlandse kolomnamen voor (`tijdstip`, `ernst`, `samenvatting`, `gegevens`). Het andere bronstuk stelde vast dat de vijf bestaande tabellen `async_tasks`, `runs`, `users`, `marked_for_deletion` en `subdomain_registry` heten, dus Engels en meervoud, en hield die lijn aan.

**Beslecht: het domeinbegrip en de klasse heten `Gebeurtenis`; de tabel- en kolomnamen volgen de bestaande huislijn, dus Engels en meervoud; en waar het NL GOV-profiel een naam voorschrijft, wint die naam.** Drie redenen, in volgorde van zwaarte:

1. Het doel van de woordkeuze was een botsing in proza en in het domeinmodel vermijden, en dat doel is bereikt met de klassenaam. De kolomnamen botsen met niets.
2. Het profiel schrijft `id`, `source`, `specversion`, `type`, `subject` en `time` letterlijk voor. Een tabel met die zes naast `tijdstip`, `ernst` en `gegevens` is half Engels en half Nederlands in een rij, en dat is slechter dan beide zuivere keuzes.
3. De vijf bestaande tabellen zijn Engels. Een zesde in een andere taal maakt elke join een leesoefening.

**Wat de verliezende redenering aandroeg en wat overeind blijft:** de Nederlandse veldenlijst was niet alleen een taalkeuze, hij was ook een veldinventaris, en hij had vijf velden die het andere bronstuk miste: `actor_soort` (mens, agent, scheduler, cluster, buiten), `samenvatting` (een mensleesbare regel in het Nederlands), `flow_id` (de bestaande correlatie-identificatie uit `opi/core/flow_id.py`), `taak_id` (een verwijzing naar `async_tasks.id` zonder vreemde sleutel, want die rij wordt na een uur verwijderd) en `component` (het derde niveau naast project en deployment). Die vijf staan in het datamodel in punt 7, met Engelse namen. En de tekst die de gebruiker leest is Nederlands: de `summary`-kolom draagt Nederlandse zinnen, en dat is waar de taalkeuze werkelijk zichtbaar wordt.

---

## De assen

Vier richtingen, alle vier op dezelfde assen. Daarna de aanbeveling, de richtingen die eerder al afvielen, en daarna de zeven vragen die binnen de gekozen richting hoe dan ook beantwoord moeten worden.

| As | Wat de vraag is |
|---|---|
| **Bouwen** | wat het kost om het de eerste keer neer te zetten |
| **Draaien** | wat het per dag kost aan opslag, schrijfwerk en onderhoud |
| **Schalen** | wat er gebeurt bij tien keer zo veel projecten of gebeurtenissen |
| **Gebruiker** | wat de persoon er werkelijk aan heeft |
| **Falen** | wat er stukgaat en hoe je dat merkt |
| **Later** | wat het blokkeert en wat het openhoudt |

---

## Richting A: doorgeefluik zonder opslag

**Wat het is.** Een gebeurtenis gaat rechtstreeks naar een kanaal (mail, Mattermost, ntfy) en verder nergens heen. Geen tabel, geen leesstatus, geen geschiedenis. De code roept bij een mislukte deploy een functie aan die een bericht opstelt en verstuurt, en daarmee is de gebeurtenis voorbij.

**Bouwen.** Het goedkoopst van de vier, met afstand. Een verzendmodule, een plek die uitrekent wie het bericht moet krijgen, en per gebeurtenis een aanroep. Er is geen migratie, geen model, geen UI. Realistisch: een PR-serie voor de eerste vijf gebeurtenissen.

**Draaien.** Bijna niets. Geen opslag, geen opruiming, geen bewaartermijn.

**Schalen.** Slecht op een manier die pas laat pijn doet. Zonder opslag is er geen ontdubbeling over herstarts heen (hetzelfde probleem als de ingebouwde logbewaker, zie deel 1 paragraaf 9), en zonder wachtrij is elke uitbarsting een uitbarsting in de mailbox van de gebruiker. Twintig herstarts is dan echt twintig mails.

**Gebruiker.** Hier valt de richting. Eerlijk opgesomd wat je niet hebt:

- **geen leesstatus**, dus geen "wat is er nieuw sinds gisteren";
- **geen geschiedenis**, dus de vraag "wat is er vannacht met mijn project gebeurd" is alleen te beantwoorden door je mailbox te doorzoeken, en "sinds wanneer is dit rood" helemaal niet;
- **geen bewijs**, dus "ik heb daar nooit iets over gehoord" is niet te weerleggen, en de BIO-kant uit punt 6 wordt er niet door bediend;
- **geen UI**, dus de eis "terug te zien in de UI en de API" uit de wens vervalt;
- **geen zinnige voorkeuren.** Dit is het subtielste verlies. Een instelling "geen mail voor uitrol" betekent zonder opslag dat die gebeurtenis voor die persoon volledig verdwijnt. De keuze is dan niet "waar wil ik het horen" maar "wil ik het weten, ja of nee", en dat is een andere en veel slechtere vraag.

**Falen.** Een kanaal dat plat ligt betekent verlies. De mailrelay die een uur weg is, kost een uur gebeurtenissen, definitief. Er is niets om opnieuw te proberen, want er is niets bewaard. En het faalt stil: niets in het systeem weet dat er iets weg is.

**Later.** Het blokkeert alles wat later gewenst is. Opslag toevoegen is geen uitbreiding maar een herbouw: de emitpunten blijven staan, maar alles wat erachter zit (route, model, aflevering, dedup, voorkeuren) wordt opnieuw gedaan. Het houdt precies een ding open: het is snel weer weg te halen.

**Waar dit wel het goede antwoord is.** Voor de logbewaker. Die stuurt vandaag naar ntfy zonder opslag, en dat is de juiste keuze voor dat geval: het publiek is een beheerder, het bericht is vluchtig, en de geschiedenis staat toch al in Loki. Richting A is dus niet fout, hij is al in gebruik voor het enige geval waar hij past.

---

## Richting B: gebeurtenissenlogboek met uitwaaieren bij lezen

**Wat het is.** Een onveranderlijke tabel met gebeurtenissen. Wie wat ziet, volgt uit een bevraging op het moment van kijken: neem de gebeurtenissen, filter op de projecten waar deze persoon lid van is, filter op de typen die hij aan heeft staan, en toon wat overblijft. Niemand krijgt een eigen rij.

**Bouwen.** Middelmatig. Een tabel, een bevraging, een UI. De autorisatie zit in de bevraging en dat is werk, maar het is werk dat er in de projectlaag al ligt (`opi/services/project_authorization.py`). Geen uitwaaierlogica bij het schrijven, dus de schrijfkant is de eenvoudigste van de drie echte richtingen.

**Draaien.** Zuinig. Een rij per gebeurtenis, hoe veel belanghebbenden er ook zijn. Voor een gebeurtenis met acht projectleden scheelt dat een factor acht ten opzichte van richting C. Bij een ruwe schatting van enkele honderden gebeurtenissen per dag over alle projecten is dat orde 100k rijen per jaar, wat voor PostgreSQL niets is.

**Schalen.** Hier zit de kwestie, en hij is tweeledig.

De bevraging zelf is te doen: een index op tijd plus een filter op een lijst projectnamen is een gewone query. Maar de **ongelezen-teller** is het probleem. Die staat in de kop van elke pagina, dus hij wordt bij elke paginaweergave gesteld, en hij is niet te beantwoorden met een gewone `count`: je moet de hele bevraging uitvoeren en er vervolgens de leesstatus overheen leggen. Bij een gebruiker is dat niets, bij honderd gebruikers die elk elke minuut een pagina laden is het de duurste query van het portaal.

En de leesstatus zelf, want die is per persoon. Er zijn drie manieren, en alle drie hebben een prijs:

1. **Een "gelezen tot"-tijdstip per persoon.** Bijna gratis, maar het is een streep en geen status: je kunt niet een melding wegklikken en de rest laten staan. Dat is minder dan wat de wens beschrijft.
2. **Een koppeltabel van persoon naar gebeurtenis.** Dan is er toch een rij per persoon per gebeurtenis, maar alleen voor wat gelezen is. Zuiniger dan richting C, en het besparingsargument van B is grotendeels weg.
3. **Een vinkje per persoon in een JSONB-veld op de gebeurtenis.** Werkt tot het aantal lezers groeit, en dan is elke leeshandeling een schrijfoperatie op een rij die anderen ook lezen.

**Gebruiker.** Bijna even goed als richting C, met een gat: **"waarom zie ik dit" is niet te beantwoorden.** De reden is de bevraging, en die is niet vastgelegd. Je kunt hem opnieuw uitrekenen ("omdat je beheerder bent van project X"), maar dan geef je de reden van NU en niet die van toen. En bij een gebeurtenis met meerdere redenen (je bent lid EN je startte de taak zelf) is er geen manier om te zeggen welke ertoe deed.

**Falen.** Het faalt op een prettige manier: als er iets misgaat in de bevraging, mist iemand een melding maar is de gebeurtenis niet weg. De tabel is onveranderlijk, dus een fout in de autorisatieregel is repareerbaar zonder gegevensverlies. Dat is een echt voordeel en het is er precies een.

**Het echte bezwaar tegen B, en dat is geen prestatiebezwaar.** De bevraging kijkt naar het HEDEN. Iemand die vandaag lid wordt van een project ziet daarmee alle gebeurtenissen van de afgelopen maanden over dat project, inclusief mislukte deploys van voor zijn tijd. En iemand die eruit gaat, verliest per direct alles wat hij ooit heeft gekregen, ook over zijn eigen handelingen. Beide zijn fout, en beide zijn niet te repareren zonder de lidmaatschapsgeschiedenis mee te nemen in de bevraging. Die geschiedenis bestaat vandaag niet in een bevraagbare vorm (lidmaatschap is een YAML-lijst; de geschiedenis is de git-log van het projectbestand, onder een vaste systeemidentiteit). Dat is een tweede systeem bouwen om het eerste te laten kloppen.

Ter herinnering uit deel 1: `cross-domain-access` maakt het nog scherper. Daar is de autorisatieregel niet "lid van het project van de gebeurtenis" maar "lid van een van de twee projecten die de gebeurtenis noemt". Elke uitzondering op de regel moet in de bevraging, en de bevraging is precies de plek waar je geen uitzonderingen wilt.

**Later.** Houdt veel open. Een onveranderlijke gebeurtenissentabel is een goede basis voor van alles, en er kan later een postvak bovenop (dan wordt B de onderlaag van C). Blokkeert niets.

---
## Richting C: postvak per persoon, het GitHub-model

**Wat het is.** Bij het ontstaan van een gebeurtenis wordt uitgerekend wie hem moet zien, en per belanghebbende wordt een rij geschreven, met leesstatus, de reden ("waarom zie ik dit") en een draadsleutel per onderwerp. Wat je ziet is wat er voor jou is neergelegd, op het moment dat het gebeurde.

**Bouwen.** Het duurst. Twee tabellen, de uitwaaierlogica, de reden per ontvanger, de draden, de voorkeurentabel, en het scherm. Realistisch: drie tot vier PR-series voor iets wat af is, al past een eerste fase (zie deel 3) in een.

**Draaien.** Duurder in opslag en schrijfwerk. Een gebeurtenis met acht belanghebbenden is acht rijen. Om te weten of dat erg is, moet je het rekenen in plaats van te vrezen. Grof geschat op de huidige omvang van het platform: enkele tientallen projecten, gemiddeld een handvol leden, en een orde van tientallen meldingswaardige gebeurtenissen per project per dag op een drukke dag. Dat komt uit op enkele duizenden rijen per dag. Een rij is orde honderden bytes. Dat is enkele honderden megabytes per jaar bij ONGELIMITEERDE bewaring, en met een bewaartermijn van negentig dagen is het een tabel van tientallen megabytes. **Dat is geen schaalprobleem, dat is een tabel.** Het schrijfwerk idem: acht `INSERT`s in een transactie is een rondgang.

**Schalen.** Het schaalt van de vier het best, en om een reden die contra-intuitief is: het duurste werk (uitrekenen wie iets moet zien) gebeurt een keer, bij het ontstaan, terwijl het vaakste werk (het postvak tonen, de teller ophalen) een indexlezing op een kolom is. De ongelezen-teller is `SELECT count(*) WHERE ontvanger = ? AND gelezen_op IS NULL`, met een partiele index precies daarop. Dat is de goedkoopste vorm die er is, en juist die query is de vaakste.

**Gebruiker.** Dit is wat de wens beschrijft. Leesstatus per persoon, geschiedenis die niet verandert als je lidmaatschap verandert, een reden bij elke melding, en draden zodat twintig gebeurtenissen over dezelfde deployment een regel in het postvak zijn.

**Falen.** Twee echte manieren.

- **De uitwaaiering is fout op het moment van schrijven, en dat is dan definitief.** Wie vergeten is, blijft vergeten: er is geen tweede kans zoals bij B, waar je de bevraging repareert en iedereen alsnog ziet wat hij moest zien. Tegenmaatregel: de gebeurtenis en de ontvangers zijn twee tabellen (zie het datamodel), dus de gebeurtenis blijft staan en de uitwaaiering is over te doen. Wat je daarbij verliest is het lidmaatschap zoals het TOEN was; dat is de reden dat deel 3 dit expliciet noemt bij de fasering.
- **De uitwaaiering staat op het kritieke pad.** Als het uitrekenen van de ontvangers faalt of traag is, raakt dat de handeling zelf. Tegenmaatregel: de outbox (zie punt 2). De gebeurtenis wordt in dezelfde transactie weggeschreven als de handeling; het uitwaaieren gebeurt erna, door de werker.

**En de vraag die de opdracht terecht stelt: wat met iemand die na het feit lid wordt, of geen lid meer is?** Het antwoord van richting C is helder en het is de reden dat C wint:

- **Wie na het feit lid wordt, krijgt de oude meldingen niet.** Ze zijn nooit voor hem neergelegd. Dat is correct: hij was er niet, hij hoefde het niet te weten, en hij hoeft de mislukte deploys van vorige maand niet als ongelezen in zijn postvak te vinden. Wil hij weten wat er is gebeurd, dan is dat de tijdlijn van het project en niet zijn postvak. Dat onderscheid tussen de tijdlijn (wat er met het project gebeurde) en het postvak (wat er voor jou is neergelegd) is precies wat twee tabellen mogelijk maken.
- **Wie geen lid meer is, houdt zijn oude meldingen.** Ze waren voor hem, hij heeft ze gekregen, en ze gaan over dingen die hij zelf deed of moest weten. Nieuwe krijgt hij niet meer, want de uitwaaiering vraagt bij elke nieuwe gebeurtenis opnieuw wie lid is. Zijn toegang tot de projectTIJDLIJN vervalt wel, want die loopt langs `is_user_authorized_for_project` en dus langs het heden; zie punt 4.
- **Twee uitzonderingen die expliciet moeten.** De eerste: bij "je bent uit dit project verwijderd" is de ontvanger op het moment van uitwaaieren juist GEEN lid meer. Die melding moet dus buiten de gewone lidmaatschapsregel om worden neergelegd, en dat kan alleen in een model dat per ontvanger schrijft. In richting B is deze melding niet te bezorgen, punt. De tweede: type 11 `platform-mededeling` (deel 1 `:523`) draagt helemaal geen project, dus de vraag "wie is lid" levert er niemand op en zonder uitzondering krijgt niemand hem. Bij die soort is de ontvangerslijst daarom het platformregister (`user_admin_service.py:23`, `list_users`; deel 1 `:288`) en niet het lidmaatschap van een project, en de ontvangerrij draagt `reason = platform-announcement`; punt 4 werkt dat uit en punt 7 zet die waarde in de lijst. **Het zijn er precies deze twee, en dat is na te rekenen.** De eerste is de enige soort waarvan de ontvanger op het moment van uitwaaieren geen lid meer IS; de tweede is de enige soort ZONDER project waarvan het publiek verder reikt dan de platformbeheerders, want in de negenenzeventig catalogusrijen van deel 1 zijn er precies twee die tegelijk publiek `A` en onderwerp `platform` dragen (`:304` en `:305`), en dat zijn dezelfde twee die deel 1 `:531` als de mededelingsrijen van type 11 aanwijst.

**Later.** Houdt het meeste open, blokkeert een ding: het is de duurste om terug te draaien. Maar de gebeurtenissentabel eronder is dezelfde als in richting B, dus terugvallen op B is een bevraging vervangen en de ontvangertabel laten staan.

---

## Richting D: het CloudEvents-jasje

**Wat het is.** Geen alternatief voor B of C maar een keuze binnen de gekozen richting: leg het gebeurtenisrecord vast in het NL GOV-profiel voor CloudEvents. Concreet betekent dat: de kolommen van de gebeurtenissentabel dragen de namen en de vorm van het profiel.

**De stand van de standaard, gemeten op 28 augustus 2026.** Op de lijst van het Forum Standaardisatie staat "NL GOV profile for CloudEvents" met versienummer **1.1** en lijststatus "Verplicht (pas toe of leg uit)", terwijl het veld "Specificatiedocument" op diezelfde pagina nog naar "NL GOV profile for CloudEvents 1.0" verwijst. De aanmelding dateert van 22 augustus 2024 en het OBDO stelde de plaatsing vast op 24 september 2025. De twee bronstukken van dit document schreven allebei dat op de lijst v1.0 staat; dat klopte bij hun meting en is inmiddels achterhaald. Voor het ontwerp verandert het niets: de vier verplichte attributen zijn in beide versies dezelfde. Wie het profiel gaat implementeren, leest de publicatie op gitdocumentatie.logius.nl en niet dit document.

Het profiel eist vier attributen:

| Attribuut | Eis van het profiel | Wat dat bij ons zou zijn |
|---|---|---|
| `id` | uniek, bij voorkeur domeinspecifiek, anders UUIDv4 | de primaire sleutel, `gen_random_uuid()` zoals elke andere tabel hier |
| `source` | URN met `nld`-namespace: `urn:nld:oin:<OIN>:systeem:<naam>` | vraagt een OIN. **Die hebben wij niet vastgesteld** en het is geen technische keuze |
| `specversion` | `"1.0"` | een constante |
| `type` | omgekeerde domeinnotatie, met `v`-suffix voor versies | `nl.rig.zad.deployment.mislukt.v1` in plaats van `deployment-mislukt` |

Optioneel maar relevant: `subject` (waar het over gaat, zodat een afnemer kan filteren zonder de payload te openen), `time` (RFC 3339), `datacontenttype`, `dataschema`, en `dataref` voor het claim check-patroon.

**Bouwen.** Vrijwel gratis ALS je het meteen doet. Het is een keuze in kolomnamen en in een naamgevingsregel voor gebeurtenissoorten. Er is een `cloudevents`-SDK voor Python, maar die is niet nodig om de vorm aan te houden; hij is nodig zodra je gebeurtenissen over HTTP uitwisselt.

**Bouwen, later.** Duur. De soortnamen staan dan in de code, in de voorkeuren van iedere gebruiker, en in de opgeslagen rijen. Omzetten is een migratie over de hele tabel plus een vertaaltabel plus een periode waarin beide vormen bestaan, en dan blijft er per soort permanent een tweede naam naast de eerste staan.

**Draaien.** Geen verschil. Een kolom heet anders.

**Gebruiker.** Nul. De gebruiker ziet dit nooit.

**Falen.** Een reeel risico: het profiel is streng over wat er in de context-attributen mag staan. "Geen gevoelige data in context-attributen, want die zijn inspecteerbaar en worden gelogd door tussenliggende systemen." Bij ons zou `subject` de projectnaam of de deploymentnaam zijn, en dat is geen persoonsgegeven. Maar het is wel de regel die je overtreedt zodra iemand het e-mailadres van de actor in `subject` zet omdat het handig staat, en onze actor IS een e-mailadres. De regel overnemen is dus zelf een voordeel, mits hij als invariant wordt afgedwongen en niet als stijlnotitie.

**Later.** Dit is het hele punt. Zonder het profiel is een koppelvlak naar buiten (een webhook, een abonnement voor een andere overheidspartij, een gemeenschappelijke notificatiedienst) een verbouwing. Met het profiel is het een endpoint.

**Wat NIET overnemen.** De Abonneren-standaard en de Notificatieservices-API. Die gaan over het aanbieden van abonnementen aan derden, en dat willen we niet en hebben we niet nodig. De werkversie van Abonneren is bovendien nog niet vastgesteld, en de Notificatieservices-repo heeft geen publicatie. Overnemen wat nog beweegt is het slechtste van twee werelden.

**Wat er open blijft staan.** De `source` vraagt een OIN (Organisatie-Identificatienummer). Dat is een vraag aan de organisatie en niet aan de bouwer; `features/local-cluster-federation.md` noemt `organization.number` alleen als doorgegeven Keycloak-attribuut en er staat nergens in deze repo een OIN. Zolang die er niet is, kan `source` een plaatshouder zijn met een vaste vorm die later een keer wordt ingevuld. Dat is precies het soort ding dat je nu goedkoop regelt en later niet meer.

---

## De vergelijking in een tabel

| As | A: doorgeefluik | B: logboek + bevraging | C: postvak per persoon | D: CloudEvents-vorm |
|---|---|---|---|---|
| Bouwen | zeer laag | middel | hoog | verwaarloosbaar (nu), hoog (later) |
| Draaien | verwaarloosbaar | laag | laag (tientallen MB) | geen verschil |
| Schalen | slecht (uitbarstingen) | matig (de teller is de duurste query) | goed (teller is een indexlezing) | geen verschil |
| Gebruiker | onvoldoende voor de wens | goed, maar "waarom zie ik dit" ontbreekt | wat de wens beschrijft | onzichtbaar |
| Falen | verlies bij een kapot kanaal | vergevingsgezind, herstelbaar | uitwaaiering is eenmalig; outbox dekt het | strengere regels rond persoonsgegevens (gunstig) |
| Later | blokkeert alles | blokkeert niets | duurste om terug te draaien | opent het koppelvlak |

## De aanbeveling

**Richting C, in de vorm van D, met de gebeurtenissentabel van B eronder.**

Dat is niet een compromis maar de constatering dat B en C dezelfde onderlaag hebben. Twee tabellen: een onveranderlijke gebeurtenissentabel (dat is B) en een ontvangertabel die per persoon een rij draagt met leesstatus en reden (dat is wat C toevoegt). Wie later B alleen wil, laat de tweede tabel leeg. De kolomnamen van de eerste tabel volgen het NL GOV-profiel (dat is D).

En de onderlaag doet meer dan het postvak dragen. De gebeurtenissentabel IS de tijdlijn per project en per deployment, en die tijdlijn is het eerste dat gebouwd wordt (deel 3): hij vraagt geen uitwaaiering, geen abonnement, geen afmeldpad, en hij kan niet mislukken bij de bezorging. Het postvak komt erbovenop zodra de gebeurtenissen die je vastlegt de goede blijken.

**Waarom A afvalt.** Omdat de wens letterlijk om leesstatus, om een UI en om per-persoon instellingen vraagt, en A geeft geen van drieen. Omdat "sinds wanneer is dit rood" en "wie deed dit" vragen zijn aan een verslag met een sleutel en een tijdsordening, en dat is een tabel. En omdat een melding die verdwijnt als de mailrelay even weg is, geen melding is. A blijft wel bestaan waar hij past: de logbewaker naar ntfy verandert niet.

**Waarom B alleen afvalt.** Om drie dingen, in volgorde van zwaarte:

1. **De melding "je bent uit dit project verwijderd" is niet te bezorgen.** De ontvanger is op het moment van kijken geen lid meer, dus de bevraging sluit hem uit. Dat is geen randgeval dat je later oplost; het is een gebeurtenis uit de inventarisatie die in richting B principieel onbezorgbaar is.
2. **De geschiedenis beweegt mee met het lidmaatschap.** Wie vandaag lid wordt, erft maandenlang ongelezen meldingen; wie vertrekt, verliest zijn eigen geschiedenis. Repareren vraagt een bevraagbare lidmaatschapsgeschiedenis, en die bestaat niet.
3. **"Waarom zie ik dit" is niet te beantwoorden**, want de reden is de bevraging en die is niet vastgelegd.

De eerste twee zijn correctheidsbezwaren en geen smaakbezwaren. Daarom valt B af als eindbeeld, en niet als onderlaag.

**Waarom D geen aparte richting is.** Omdat hij niets kost als je hem meeneemt en veel kost als je hem overslaat. Het enige wat hij nu vraagt is een besluit over de `source`-URN en een naamgevingsregel voor gebeurtenissoorten. Neem het niet blind over: de Abonneren-standaard en de notificatiedienst-API blijven buiten de deur, en het profiel wordt gevolgd op het RECORD, niet op de architectuur. Wat dat precies betekent staat hieronder, want daar zat de scherpste tegenspraak tussen de twee bronstukken.

---

## De richtingen die eerder al afvielen

Deze vier zijn in het andere bronstuk gewogen en afgevallen voordat A tot D op tafel lagen. Ze staan hier omdat ze anders opnieuw worden voorgesteld, en omdat drie van de vier wel als AANVULLING overeind blijven.

**Alleen gestructureerd loggen naar Loki.** De logregels krijgen een vaste JSON-vorm met velden in plaats van proza, en de retentie is die van Loki. Kost weinig: `flow_id` staat al in elke regel (`opi/utils/logging_config.py:48`, `opi/core/flow_id.py`), de logger is overal, en de log watcher leest al uit Loki. *Valt af als enige oplossing, om drie redenen.* De Loki-, Grafana- en Mimir-stack staat niet in deze repo maar wordt geleverd (deel 1, "meld- en exportinfrastructuur"), dus de retentie is niet te verifieren en niet in ons beheer. Een tijdlijn per project renderen uit Loki betekent dat de portal afhankelijk wordt van een externe dienst voor een gewone pagina. En de autorisatie klopt niet: Loki kent onze projectrollen niet, dus lezen zou langs een tweede regel lopen in plaats van langs `is_user_authorized_for_project`. *Blijft staan als aanvulling:* gestructureerd loggen is goedkoop en maakt de log watcher beter.

**OTLP als bron van waarheid.** Alle negen `opentelemetry-*`-pakketten staan in `operations-manager/python/pyproject.toml:66-74` en `opi/core/tracing.py` is compleet, dus het lijkt gratis. *Valt af.* `opi/core/tracing.py` importeert uitsluitend `OTLPSpanExporter`: er is een trace-exporter en geen log- of metriekexporter, dus een gebeurtenis zou als span moeten worden weggeschreven, wat semantisch scheef is (een gebeurtenis heeft geen duur). Er staat geen ontvanger: `OTEL_EXPORTER_OTLP_ENDPOINT` wijst naar `http://jaeger.rig-system:4317` (`opi/core/config.py:274`) en er staat geen Jaeger in `infrastructure/bootstrap/infrastructure/`. En een tracingbackend is geoptimaliseerd voor bemonsterde, kortlevende spans, niet voor een volledig, jarenlang bewaard verslag. *Blijft staan als exportweg, niet als bron van waarheid.*

**Kubernetes-events op de projectnamespace.** OPI schrijft een `Event`-object in de namespace van het project. Laag qua code (de kubectl-connector kan het), hoog qua eigenschappen. *Valt af.* De standaardretentie van Kubernetes-events is een uur (de `--event-ttl`-standaard van de kube-apiserver; **niet geverifieerd** op ODCN, want die instelling staat niet in deze repo), wat het probleem verplaatst en niet oplost. De events zijn zichtbaar voor iedereen met leesrechten op de namespace, wat niet dezelfde kring is als de projectleden. Platformgebeurtenissen hebben geen namespace om in te landen. En de gebruiker van ZAD kijkt niet in een namespace; hij kijkt in de portal. *Blijft staan als bron, niet als opslag:* Kubernetes-events zeggen dingen die wij niet zelf weten (image-pull-backoff, OOMKilled, probe-kills) en worden al per taak opgehaald.

**Abonnementen in het projectbestand.** Voor: het staat waar de rest van de projectconfiguratie staat, het is via de wizard en de API te bewerken met de bestaande editables, het is versiebeheerd, en een abonnement verdwijnt vanzelf als het project verdwijnt. *Valt af als hoofdregel.* Elke wijziging is een commit in `zad-projects` met een AGE-hercodering van de geheimen erin (een webhook-geheim moet versleuteld), een GitOps-diff, en een herverwerking van het project. Een gebruiker die zijn e-mailmelding uitzet veroorzaakt daarmee een deployment-cyclus, en dat is een absurde verhouding tussen oorzaak en gevolg; het is ook een afmeldpad met een drempel, en dat is in punt 6 een AVG-argument en niet alleen een ergernis. *De uitzondering die wel geldt:* zodra een abonnement infrastructuur nodig heeft, dus een uitgaande NetworkPolicy en een geheim, hoort het aan-uit-vinkje in het projectbestand, precies zoals `vlam` en `send-email` dat doen (`features/vlam-service.md`, `features/send-email.md`). De ontvanger-URL en het bezorgbeleid kunnen dan nog steeds in de database staan. Het tegenargument dat de database niet uit git te herbouwen is, telt hier minder zwaar dan elders: een verloren abonnement betekent dat iemand een melding mist, niet dat een applicatie niet draait.

---

## Beslecht: CloudEvents als intern opslagformaat

**De tegenspraak.** Het ene bronstuk beveelt aan het NL GOV-profiel te volgen OP HET RECORD, dus op de kolomnamen van de gebeurtenissentabel, en de Abonneren-standaard en de notificatiedienst-API buiten de deur te laten. Het andere verwerpt CloudEvents juist ALS INTERN OPSLAGFORMAAT, met twee redenen: het profiel vraagt een OIN die nergens in deze repo staat, en het verbiedt persoonsgegevens in de context-attributen terwijl onze actor een e-mailadres is. Het accepteert het alleen als projectie op de rand, met een interne tabel die eigen namen draagt maar waarvan `type` al reverse-DNS-waarden bevat zodat de projectie een hernoeming is en geen vertaling.

Beide redeneringen zijn opgeschreven en beide zijn houdbaar. Ze moeten hier beslecht worden, want ze leveren een ander schema op.

**Beslecht: het profiel wordt gevolgd op het record, dus de kolommen dragen de namen van het profiel, en de twee bezwaren van de andere kant worden bindende regels in plaats van redenen om het niet te doen.**

Concreet, en dit zijn drie regels en geen adviezen:

1. **`source` draagt tot nader order een plaatshouder van vaste vorm, en een record met een plaatshouder-`source` mag niet geexporteerd worden.** De OIN-beslissing is een organisatievraag (zie deel 3). Zolang die openstaat is het record CloudEvents-VORMIG maar geen geldig CloudEvent, en dat mag ook nergens zo genoemd worden, ook niet in een Verklaring van Toepasselijkheid. De exporteur weigert een plaatshouder; dat is een controle en geen intentie. **Wat "exporteren" hier betekent, en de enige uitzondering.** Exporteren is de projectie naar buiten die zich AANDIENT als CloudEvent: de exporteur uit deel 3 fase 9, richting een partij die zich in de zin van het profiel op onze gebeurtenissen abonneert. De webhook per project (deel 3, Kanaal 5) valt daar niet onder. De ontvanger daarvan is de eigen tooling van het project, dezelfde partij die vandaag al de `X-API-Key` van dat project houdt, en er is geen standaard in het spel die iets belooft; dat is precies het onderscheid dat deel 3 bovenaan maakt tussen die webhook en een uitgaand abonnement naar derden. Zonder deze uitzondering zou het eerste push-kanaal achter de OIN komen te staan, dus achter een organisatievraag, en dat is nu juist de reden waarom het het eerste kanaal is. De uitzondering heeft een prijs, en dat is dezelfde regel van de andere kant: wat daar de deur uit gaat mag zich niet als CloudEvent AANDIENEN. Concreet, en dit is toetsbaar: content-type `application/json` en niet `application/cloudevents+json`, de plaatshouder blijft herkenbaar een plaatshouder, en de documentatie van de webhook noemt de body CloudEvents-VORMIG en geen CloudEvent. Zodra de OIN er is vervalt het onderscheid vanzelf.
2. **Geen persoonsgegevens in de context-attributen, afgedwongen op het schema.** `subject` draagt `<project>` of `<project>/<deployment>`, nooit een e-mailadres. De actor staat in een eigen kolom die geen context-attribuut is en die door de exporteur in `data` terechtkomt, niet in `subject`. Dit is een invariant met een toets erbij, niet een conventie.
3. **`type` staat meteen in reverse-DNS-notatie met een `v`-suffix**, uit een gesloten enum in code, zoals `TaskType` dat is. VOORSTEL voor de vorm: `nl.rig.zad.<domein>.<gebeurtenis>.v1`.

**Waarom deze kant en niet de andere.** Het argument van de verliezende kant is dat de twee dezelfde vorm hebben zodra je de kolommen goed kiest, en dat de kosten van het NIET doen een migratie over de hele tabel zijn om `type` te hernoemen. Dat argument is juist, en het pleit sterker voor de winnende kant dan voor de eigen: de namen van het profiel meteen overnemen is dezelfde zet, maar dan zonder de hernoeming die overblijft. Regel 1 en 2 hierboven vangen precies de twee bezwaren op, dus er gaat niets verloren dat de andere kant beschermde.

**Wat de verliezende redenering aandroeg en wat overeind blijft:**

- De vaststelling dat het record vandaag GEEN geldig CloudEvent is en dat niet mag heten. Dat is regel 1 geworden, en zonder die redenering was het een voetnoot gebleven.
- De vaststelling dat een naieve projectie die de actor in `subject` zet het profiel schendt. Dat is regel 2 geworden, met een toets, en niet de zin "de actor hoort in de payload" die het andere bronstuk erover schreef.
- De eis dat de projectie triviaal moet zijn. Die is nu triviaal geworden op een andere manier: er valt niets meer te projecteren behalve `source` invullen.
- De constatering dat het profiel een centraal register voor gebeurtenissoorten vraagt. Dat register is bij ons de gesloten enum in code, en dat moet ergens opgeschreven staan als het antwoord op die eis.

**En wat er niet mee wordt overgenomen, ongewijzigd:** de Abonneren-standaard (werkversie v0.0.1, niet vastgesteld) en de Notificatieservices-API. Het profiel wordt gevolgd op het RECORD, niet op de architectuur.

*Bron en voorbehoud bij de profielbeschrijving:* de eisen van het profiel zoals hierboven samengevat komen uit de skill `standaarden:ls-notif` (plugin `standaarden` 0.3.9), die zichzelf als concept aanmerkt en niet de normatieve tekst is, aangevuld met de lijstgegevens van forumstandaardisatie.nl zoals gemeten op 28 augustus 2026. De publicaties op forumstandaardisatie.nl en gitdocumentatie.logius.nl zijn leidend; wie het profiel daadwerkelijk gaat implementeren, leest die eerst.

---

Wat hieronder volgt geldt binnen de aanbevolen richting.

## 1. Waar de gebeurtenis ontstaat

**Dit is de belangrijkste architectuurkeuze in het hele stuk.** Er zijn drie manieren en de verleiding is om de mooiste te kiezen.

### De drie manieren

**a. Losse `emit()`-aanroepen door de code heen.** Op de plek waar iets gebeurt staat een regel die een gebeurtenis vastlegt. `await emit(GebeurtenisSoort.DEPLOYMENT_MISLUKT, project=..., ...)` (VOORSTEL).

**b. Declaratief via het bestaande hakensysteem.** Een dienst declareert zijn eigen gebeurtenissen zoals hij nu al zijn eigen goedkeuringen declareert: `@on(ActionEvent.X)` in `opi/services/catalog/events.py`.

**c. De opslagweg vergelijkt.** Bij het opslaan van een projectbestand wordt oud tegen nieuw gelegd en daar rollen gebeurtenissen uit.

### Waarom b vandaag niet kan, en dat is een meting en geen mening

Het hakensysteem is echt en het is goed gebouwd: een decorator, een index in de registry, een payload-object per event, en `features/service-event-hooks.md` beschrijft het contract. Maar `ActionEvent` heeft **twee** leden: `AFTER_SYNC` en `REDEPLOY` (`opi/services/services_enums.py:176-177`, klasse op `:149`). En `UIEvent` heeft er drie (`:201-203`), alle drie over weergave.

Dat is geen gebeurtenissenbus. Het is een uitbreidingspunt in de uitrolcyclus: `AFTER_SYNC` vuurt een keer per deployment na de synchronisatie, `REDEPLOY` per component waar nieuwe inhoud op is gezet (`_EVENT_LEVELS`, `services_enums.py:220`). Bijna geen enkele gebeurtenis uit de inventarisatie past daarop. Een mislukte backup, een goedgekeurde aanvraag, een verwijderd projectlid, een verlopen console, een geweigerde API-sleutel: geen ervan gebeurt in de uitrolcyclus.

Er is bovendien een contract dat botst. Een `ActionEvent`-handler **commit nooit**; de aanroeper doet een commit voor de hele ronde. Dat is precies goed voor "muteer het projectbestand" en precies verkeerd voor "leg een gebeurtenis vast", want een gebeurtenis moet juist meecommitten met de handeling die hem veroorzaakte (zie de outbox in punt 2).

**Conclusie: het hakensysteem uitbreiden met een derde familie is een echte optie, maar het is een verbouwing van dat systeem en geen gebruik ervan.** En de verbouwing levert alleen iets op voor de gebeurtenissen die uit een DIENST komen. Dat zijn er in de inventarisatie een handvol. De rest komt uit de takenwerker, de planners, de goedkeuringsmachine, de middleware en de opslagweg, en die zijn geen dienst.

### Wat wel

**Aanbeveling: a als basis, c voor het projectbestand, en b alleen als een dienst iets weet dat niemand anders weet.**

- **a. Een losse aanroep, op de plek waar het gebeurt.** Niet mooi, wel eerlijk. Het is de enige vorm die op alle bronnen uit de inventarisatie werkt, en hij is direct te lezen: op de regel waar de taak faalt, staat dat er een gebeurtenis uit komt. De prijs is dat een vergeten aanroep een stille lacune is. Die prijs is beheersbaar door de aanroepen te bundelen op de weinige plekken waar de levenscyclus samenkomt: de takenwerker (een plek voor 23 taaksoorten keer drie eindtoestanden), de goedkeuringsmachine (`apply_approval_verdicts`, een plek voor alle oordelen), de gezondheidsbewaker, en de autorisatiemiddleware voor de beveiligingsgebeurtenissen.
- **c. Voor het projectbestand: vergelijk oud tegen nieuw.** Ledenwijzigingen, rolwijzigingen en dienstwijzigingen zijn geen aanroep maar een verschil. Op de opslagweg (`opi/web/router_detail_edit.py`, `save_and_commit_project`) staan beide versies ter beschikking. Een vergelijkingsfunctie die een lijst gebeurtenissen teruggeeft, dekt alle gebeurtenissen die uit het projectbestand komen in een keer, ook toekomstige. Dit is de enige plek waar declaratief werk echt loont.
- **b. Alleen waar de dienst het weet.** `notices_for` in de `ApprovalSpec` is er al: de dienst schrijft de zin die de aanvrager te lezen krijgt, want alleen de dienst kent het gevolg. Als een dienst een eigen gebeurtenis heeft met een tekst die alleen hij kan schrijven, hoort dat op dezelfde manier. Dat is een uitbreiding van het dienstcontract en het is klein te houden.

**Wat hier NIET moet gebeuren.** Geen abstractie bouwen die alle drie de vormen achter een gezicht verstopt. Ze zijn wezenlijk verschillend (een aanroep, een vergelijking, een declaratie) en een gezicht maakt alleen dat je bij het lezen niet meer ziet welke het is.

## 2. Betrouwbaar afleveren

**De outbox.** De gebeurtenis wordt geschreven in **dezelfde transactie** als de handeling die hem veroorzaakte. Faalt de handeling, dan is er geen gebeurtenis. Slaagt de handeling, dan staat de gebeurtenis er, ook als de aflevering later stukloopt. Dat is de enige manier om "twee dingen die allebei of geen van beide moeten gebeuren" te krijgen zonder een tweede systeem.

Dat werkt hier omdat de meeste handelingen al door Postgres gaan: de takenrij, de runs, de markeringen. Voor de handelingen die naar git schrijven (het projectbestand) is het niet atomair, en dat moet je gewoon opschrijven: de commit slaagt en de gebeurtenis faalt is een denkbare toestand. De juiste volgorde is dan: eerst committen naar git, dan de gebeurtenis schrijven, want een gemiste gebeurtenis is minder erg dan een gebeurtenis over iets wat niet gebeurd is.

**Wie hem leegdrinkt.** Twee kandidaten en de keuze is niet vanzelfsprekend.

*De bestaande takenwerker* (`opi/core/task_worker.py`) draait al, heeft al een hoofdlus, een hartslag, een herstellus voor vastgelopen taken en een opruimlus. Hem aanhaken is het minste werk. Maar: hij verwerkt precies een taak tegelijk en de taken zijn zwaar (een deployment duurt minuten). Een melding zou dan achter een uitrol in de wachtrij staan. Dat is niet acceptabel voor iets wat binnen seconden hoort aan te komen.

*Een eigen planner in de lifespan van `server.py`* staat naast de zeven die er al staan (de backupplanner, de stemmer, de reconciliatie, de consolereaper, de logbewaker, de slaapstandsweeper en de federatiedienst, `opi/server.py:214` en verder; deel 1 heeft de volledige lijst). Dat is het bekende patroon van dit codebestand, elke planner is een tiental regels, en hij is onafhankelijk in te stellen en uit te zetten.

**Aanbeveling: een eigen planner.** Dezelfde vorm als de bestaande, met een korte tik (een paar seconden) en een partiele index op wat nog niet is afgeleverd. Voordeel boven de takenwerker: hij loopt niet vast achter een uitrol, en hij is los uit te schakelen zonder de takenrij te raken.

**Let op de valkuil die dit codebestand zelf al heeft gedocumenteerd.** Een planner in de lifespan draait per proces. Draaien er meerdere OPI-processen, dan drinken ze allemaal dezelfde outbox leeg en wordt elke melding meerdere keren afgeleverd. De takenrij heeft dit opgelost met een claimmechanisme (`claim_next_task`, `async_task_service.py:182`) plus een `instance_id`. De outboxplanner moet hetzelfde doen: claim met `FOR UPDATE SKIP LOCKED` of het equivalent daarvan dat de takenrij al gebruikt. Dit is geen theoretisch punt; de websocket-router noteert al dat zijn eigen limieten per werker gelden.

**Opnieuw proberen.** Per aflevering (niet per gebeurtenis) een pogingsteller en een `next_attempt_at`. Exponentieel oplopend met een plafond, in de orde van vijf minuten. Na een vast aantal pogingen: opgeven, de aflevering markeren als mislukt, en dat zelf als gebeurtenis vastleggen voor de platformbeheerder. Een kanaal dat structureel niets aanneemt, hoort een storing te zijn en geen stilte.

**Idempotentie.** Twee lagen, want ze dekken verschillende fouten:

- **Bij het aanleggen van de aflevering**: een uniciteitsgrendel op (aflevering, kanaal). Twee keer dezelfde aflevering plannen levert een rij op.
- **Bij het afleveren**: een aflevering gaat van `pending` naar `claimed` naar `sent`/`failed`, en de overgang naar `claimed` is de claim. Een werker die halverwege omvalt, laat een aflevering in `claimed` staan; die wordt na een tijdsdrempel teruggezet, net zoals `recover_stale_tasks` dat voor taken doet. Dat betekent dat een bericht in het ergste geval twee keer aankomt in plaats van nul keer, en dat is de goede kant om op te falen. Een gat is erger dan een herhaling: een herhaling is irritant, een gat is een gemiste storing.

**Wat als een kanaal plat ligt.** Het postvak en de tijdlijn zijn geen kanaal in deze zin: die rijen staan er al, want die zijn de gebeurtenis. De push-kanalen kunnen wel falen. Mail en Mattermost zijn afleveringen in de zin van deze outbox en stapelen zich daar op: als de mailrelay een uur weg is, komen na dat uur alle berichten alsnog. Voor de gebruiker betekent dat een stapel; dat is een reden om per persoon per tijdvak samen te vatten (zie punt 3 en deel 3, "E-mail"). De webhook per project kan even goed falen, maar hij is geen aflevering in deze zin en loopt niet over deze outbox: hij hangt aan een projectabonnement en niet aan een persoon, en zijn herhaling loopt op het watermerk uit punt 3. Zie punt 7.

## 3. Ontdubbelen, samenvoegen en drempels

Dit is niet een probleem maar vijf, en ze vragen om verschillende antwoorden: ernst, ontdubbelen, samenhang tussen gebeurtenissen over hetzelfde onderwerp, samenvoegen richting de push-kanalen, en wat er bij een herstart gebeurt. Dat ruisonderdrukking echt werk is en geen theorie, blijkt uit de log watcher: die heeft er vijf lagen voor nodig gehad om bruikbaar te blijven op een systeem dat een ding doet (deel 1, paragraaf 9). Een gebeurtenissensysteem met vijftien soorten heeft ze allemaal nodig, en het is goedkoper ze in het ontwerp te zetten dan er later omheen te bouwen.

### Ernst

**Drie niveaus, niet vijf.** Vijf niveaus leiden ertoe dat niemand het verschil tussen twee middenniveaus kan uitleggen, en dan glijdt alles naar boven. De drie zijn in het schema `informational`, `actionable` en `outage`, en op het scherm heten ze *ter informatie*, *actie nodig* en *storing*.

Die drie zijn niet verzonnen: `event_interpreter.EventSeverity` (`opi/services/event_interpreter.py:18`) classificeert al op `actionable` / `informational` / `noise` voor de hele vertaaltabel van Kubernetes-redenen, en die classificatie is beproefd. Overnemen dus, met `noise` eruit: wat ruis is, wordt geen gebeurtenis. `outage` is de derde die daarbij komt, voor het geval waarin iets niet werkt.

**Beslecht: het middelste niveau heet op het scherm *actie nodig*, niet *aandacht*.** De twee bronstukken gaven `actionable` een verschillende Nederlandse naam, en allebei die namen zijn de samenvoeging in gereisd. Deel 1 schrijft *actie nodig*: in de leeswijzer, in de vertaalregel `actionable` naar actie nodig, en in dertig datarijen verdeeld over de tien catalogustabellen. Deze paragraaf schreef *aandacht*. Omdat de andere twee namen wel woordelijk samenvielen, las dat als hetzelfde drietal met twee namen voor het midden, en dan weet een lezer niet welke geldt. *Actie nodig* wint om twee redenen. Het zegt wat het niveau betekent, namelijk dat er iemand iets moet doen, en dat is ook de letterlijke lezing van `actionable`; *aandacht* zegt alleen dat je moet kijken en zakt daarmee naar het niveau eronder. En het is de naam die de catalogus draagt, dus de andere keuze zou dertig rijen hernoemen om een enkele zin te sparen. Wat de verliezende kant aandroeg blijft staan, namelijk de omschrijving die daar bij de naam hoorde: dit niveau betekent dat iemand er iets mee moet, maar dat het kan wachten. Dat is precies het verschil met *storing*, waar iets niet werkt en het niet kan wachten.

**De ernst hoort bij de gebeurtenisSOORT, vast in code, niet per geval bepaald.** Een gebeurtenis waarvan de ernst per geval verschilt, zijn eigenlijk twee soorten. Dat spreekt de regel "de ernst is geen type" uit deel 1 niet tegen, want daar gaat het over de twaalf CATEGORIEEN waar een gebruiker een knop voor omzet: `nl.rig.zad.uitrol.mislukt.v1` en `nl.rig.zad.uitrol.geslaagd.v1` zijn twee soorten met een vaste, verschillende ernst binnen dezelfde categorie `uitrol`. Soort en categorie zijn twee dingen, en alleen de categorie staat in het voorkeurenscherm.

### Beslecht: waar er wordt ontdubbeld

**De tegenspraak.** Het ene bronstuk legt de dedup op het VASTLEGGEN: een `dedup_key` met een grendel binnen een venster, en als dezelfde sleutel terugkomt hoog je `occurrences` op de bestaande gebeurtenis op en zet je `last_seen_at` bij. Het andere legt hem uitdrukkelijk op het MELDEN: alles wordt vastgelegd, wat wordt gemeld is een afgeleide, en dedup bij het melden is een `GROUP BY` over een venster. Zijn reden: als je bij het vastleggen dedupliceert, kun je achteraf niet meer vaststellen hoe vaak iets gebeurde, en "vijftig keer in een uur" is precies het signaal dat je wilt hebben.

**Beslecht: alles wordt vastgelegd; de deduplicatie en de samenvoeging gebeuren aan de meldkant.** Drie redenen, en de derde is de doorslaggevende:

1. Vijftig herstarts zijn vijftig feiten met vijftig tijdstempels. Een rij met `occurrences = 50` weet niet meer wanneer nummer 12 was, en dus ook niet of ze in tien minuten of in tien uur gebeurden. Dat verschil is de melding.
2. De tijdlijn per deployment is de plek waar "sinds wanneer is dit rood" beantwoord wordt. Die vraag is een `ORDER BY` over rijen, niet over een teller.
3. **Het botst met de onveranderlijkheid die punt 5 zelf eist.** Diezelfde aanbeveling schrijft voor dat de gebeurtenissentabel alleen-invoegen is, nooit bijwerken, nooit verwijderen binnen de bewaartermijn, en beroept zich daarvoor op de bewijseis. Een `occurrences`-teller die bij elke waarneming wordt opgehoogd is een `UPDATE` op precies die tabel. Dat is een tegenspraak binnen een van de twee bronstukken, en niet alleen tussen de twee.

**Wat de verliezende redenering aandroeg en wat overeind blijft:** bijna alles, alleen een laag hoger.

- **De `dedup_key` blijft, op de gebeurtenis.** Hij is een eigenschap van de soort, wordt bij het schrijven berekend en daarna nooit meer aangeraakt, en hij is de sleutel waarop de meldkant groepeert. Wat vervalt is de uniciteitsgrendel: er is geen grendel, want er is niets om tegen te houden. Op de ontvangerrij staat hij daarnaast nog een keer als gedenormaliseerde kopie, want daar draait de tellervraag op (punt 7).
- **De vorm van de sleutel blijft.** `signature()` (`log_watcher.py:312`) normaliseert een melding tot een stabiele sleutel door tijdstempels, IP-adressen, gekoppelde identifiers en losse getallen weg te strippen, tot maximaal 120 tekens. Die functie is beproefd en wordt overgenomen. De toestand hoort in Postgres en niet in het geheugen; dat is de fout die in de bestaande planner zit (deel 1, paragraaf 9).
- **`occurrences` en `last_seen_at` blijven ook, maar op de ontvangerrij.** Daar mag wel worden bijgewerkt (`read_at` en `archived_at` worden daar sowieso gezet), en daar horen ze semantisch ook: het is niet een gebeurtenis die vaker voorkwam, het is een MELDING die vaker bevestigd werd.
- **De venstervorm blijft, en die is beter dan een vast getal.** De logbewaker staat op zes uur en dat is voor ops-alarmen verdedigbaar. Voor een postvak is dat te lang: als je een melding om negen uur leest en om elf uur gaat hetzelfde weer mis, hoor je dat te zien. **Het venster loopt daarom tot de melding gelezen is, met een plafond.** Ongelezen plus dezelfde sleutel betekent optellen op de bestaande ontvangerrij; gelezen betekent een nieuwe ontvangerrij. Dat is precies hoe een mens erover denkt en het vraagt geen instelbare duur.

### Verschillende gebeurtenissen over hetzelfde onderwerp

Een uitrol die faalt, gevolgd door een automatische stemming, gevolgd door een geslaagde uitrol. Drie gebeurtenissen, een verhaal. Antwoord: een **draadsleutel** (`thread_key` in het datamodel), die het ONDERWERP benoemt en niet de gebeurtenis: `deployment:<project>/<deployment>`, `aanvraag:<project>/<dienst>/<sleutel>`. Het postvak groepeert op draad. Dat is wat GitHub met een issue-draad doet, en het is de reden dat een druk project daar leesbaar blijft. De draadsleutel staat op de gebeurtenis, want hij hoort bij het onderwerp en niet bij de ontvanger.

### Samenvoeging aan de kant van de push-kanalen

Voor het postvak is de draad genoeg. Voor de push-kanalen niet, want daar gaat elk bericht als een eigen bezorging de deur uit: bij mail en Mattermost als een aflevering in de outbox, bij de webhook als een aanroep op het watermerk. **Een melding per ontvanger per venster, met de gebeurtenissen erin gegroepeerd,** en niet een melding per gebeurtenis. Dit is dezelfde keuze die de log watcher al maakt (een ntfy-bericht met maximaal tien regels, gegroepeerd, `MAX_BODY_LINES = 10`) en om dezelfde reden: het aantal dat telt is het aantal berichten, niet het aantal gebeurtenissen.

### Wat er gebeurt bij een herstart

De log watcher heeft hier vandaag een bekend gat: `self._state: dict[str, str] = {}` in `opi/core/logwatcher_scheduler.py:32`, met het commentaar "it resets on an OPI restart, which at worst repeats one alert". Voor een ntfy-topic is dat aanvaardbaar. Voor mail naar gebruikers is het dat niet: een herstart tijdens een uitrol zou iedereen een dubbele mail sturen.

**Geen dedup-toestand in het geheugen.** De laatst gemelde stand hoort voor elk push-kanaal in dezelfde database als de gebeurtenissen, maar hij staat niet voor elk kanaal op dezelfde plek, en dat volgt uit punt 7. Voor mail en Mattermost is die stand de outboxrij zelf: die is er al voordat de bezorging begint, hij overleeft de herstart, en een rij die in `claimed` bleef staan wordt na een tijdsdrempel teruggezet. Voor de webhook per project, die geen ontvangerrij en dus geen outboxrij heeft, is het een watermerk per abonnement (VOORSTEL: `reported_through`, een tijdstip). Melden is daar "alles sinds het watermerk, gegroepeerd", en het watermerk schuift pas op na een geslaagde bezorging. In beide vormen levert een herstart midden in een melding hooguit een herhaling van een venster op, en een bezorging die mislukt een herhaling in plaats van een gat.

**Een absolute bovengrens per ontvanger en per abonnement per dag,** zodat een lus die duizend gebeurtenissen produceert niet duizend meldingen produceert. Bij overschrijding: een melding die zegt dat de grens is geraakt, met een verwijzing naar de tijdlijn. Dit is een noodrem, geen beleid.

## 4. Wie mag welke gebeurtenis zien

Een gebeurtenis draagt projectscope, dus lezen loopt langs `is_user_authorized_for_project` in `opi/services/project_authorization.py:40`, en niet langs een eigen tweede regel. Dat is niet alleen netjes maar noodzakelijk: de rollen komen uit het projectbestand via de ProjectStore, en een tweede kopie van die logica loopt gegarandeerd uit de pas op het moment dat iemand een lid verwijdert. De trage reconcile-poll (`opi/services/project_store.py:1343`) bestaat precies omdat een intrekking binnen een begrensd venster moet doorwerken; een tweede autorisatieweg zou dat venster stilzwijgend verlengen.

Gebeurtenissen zonder project zijn platformgebeurtenissen en zijn alleen voor beheerders, op een uitzondering na. Er is geen tussenvorm: een gebeurtenis die "een beetje" van een project is, is een ontwerpfout in de gebeurtenissoort. **De uitzondering is het omgekeerde van een tussenvorm, en daarom staat zij hier en niet alleen bij het kanaal: type 11 `platform-mededeling` (deel 1 `:523`, publiek `A en B`).** Zo'n mededeling is niet een beetje van een project maar van het hele platform, en haar publiek is dus RUIMER dan de beheerders en niet smaller. Deel 1 zegt dat ook op drie plekken: "er is een nieuwe release van het platform" (`:304`) en "onderhoud is gepland" (`:305`) dragen belanghebbende `iedereen`, beide dragen publiek `A en B`, en `:523` geeft type 11 als standaard postvak plus mail voor de projectbeheerder EN het projectlid. Deel 3 `:377` hangt er zijn rolprofiel aan op: mededelingen van het platform zijn de enige mail die het projectlid standaard krijgt. **Dat het er precies een is, is te meten**: van de negenenzeventig catalogusrijen dragen er twee tegelijk publiek `A` en onderwerp `platform`, `:304` en `:305`, en dat zijn dezelfde twee die deel 1 `:531` als de mededelingsrijen van type 11 aanwijst. **Van de andere rijen met publiek `A` valt er geen buiten een project.** Parse de tien catalogustabellen van deel 1, los een `idem`-cel op naar de rij erboven en houd over wat publiek `A` draagt: op die twee na dragen ze allemaal `deployment`, `project`, `component`, `dienst binnen een project` of een samenstelling daarvan, met een enkele rij op onderwerp `dienst` (`:230`, de definitief verwijderde markering), en die legt deel 1 `:529` uitdrukkelijk bij de resource van een project en niet bij de sweep zelf. **Wat de uitzondering praktisch betekent**, want dat is de helft die anders wordt vergeten: de uitwaaiering uit richting C (`:134`) vraagt bij deze soort niet wie lid is van een project maar leest het platformregister (`user_admin_service.py:23`, `list_users`; deel 1 `:288`), en de ontvangerrij draagt `reason = platform-announcement`, de zesde waarde van de gesloten lijst in punt 7. Beide leeswegen doen mee: de mededeling ligt in het postvak van elke gebruiker en staat op de platformtijdlijn; op een projectTIJDLIJN staat hij niet, want hij draagt geen project. **Dit wijkt bewust af van het startpunt van de opdracht** (`plans/wat-er-gebeurt-vastleggen-en-melden.md:94`), dat "Platformgebeurtenissen zijn alleen voor beheerders" zonder voorbehoud stelt. Dat is geschreven voordat de catalogus er was, en juist die catalogus, die dezelfde opdracht als eerste deliverable vroeg, levert twee rijen op die geen project dragen en toch belanghebbende `iedereen` hebben. Waar de opdracht en de meting botsen wint de meting, en de afwijking staat hier zodat zij niet stilzwijgend is. En hij komt niet van buiten binnen: deel 3, Kanaal 2, ontwerpregel 6 weert deze soort op het inkomende endpoint juist omdat een enkele schrijfhandeling hier elke gebruiker bereikt, en zet er een eigen route voor een platformbeheerder met identiteit naast.

**Maar de projectscope is de eerste grendel en niet de enige: het publiek is de tweede.** Er zijn namelijk gebeurtenissen die WEL een project dragen en toch niet voor het project zijn. **Geteld op de projectSCOPE, en uitdrukkelijk niet op de kolom `onderwerp`:** die kolom zegt volgens de legenda van deel 1 (`:49`) waar een gebeurtenis OVER gaat, en dat is een andere eigenschap dan de vraag of hij aan een project hangt. Zo geteld kent deel 1 er zes, alle zes met publiek `B` en zonder `A` of `C`. Vier bestaan vandaag: de weggegooide in-memory wizardtaak (deel 1 `:141`), de ingediende aanvraag (deel 1 `:175`), de toegekende projectrol (deel 1 `:278`) en de doorgestuurde federatietaak (deel 1 `:301`). Twee staan in de catalogus als "bestaat nog niet": een aanvraag die al lang openstaat (deel 1 `:178`) en een ingetrokken aanvraag (deel 1 `:179`). **Zo is dat na te rekenen, en de twee randgevallen zitten er met opzet in.** Parse de tien catalogustabellen van deel 1, los een `idem`-cel op naar de rij erboven en houd over wat publiek uitsluitend `B` draagt: zevenentwintig rijen. Zestien daarvan hebben onderwerp "platform" en vallen meteen af. Vijf noemen een project in hun onderwerp en tellen mee: `:141` en `:301` ("project"), en `:175`, `:178` en `:179` ("dienst binnen een project"; die laatste twee schrijven "idem", dus zonder het oplossen daarvan ziet een sweep op het woord "project" ze niet, en daarom staan ze hier bij naam). Twee hebben onderwerp "dienst" en tellen NIET mee: dat zijn de weessweep-rijen `:229` en `:231`, en deel 1 `:529` legt ze uitdrukkelijk bij de sweep zelf en niet bij een project. Blijven over vier rijen met onderwerp "gebruiker". Drie daarvan gaan over het platformregister (`:292`, `:293` en `:294`, `opi/services/user_admin_service.py`) en dragen geen project. De vierde is `:278`, "een projectrol is aan een realm-gebruiker toegekend", en die draagt er wel een: hij ontstaat op `realm_name` in `opi/manager/invite_manager.py:187` en `:153`, en dat is bij ZAD de realm van een project (de docstrings van allebei de aanroepers zeggen het letterlijk, `:281` en `:375`: "The project's Keycloak realm name"). Zijn vijf buren uit dezelfde uitnodigingsketen (deel 1 `:270` tot en met `:274`) dragen allemaal onderwerp "project", punt 6 hieronder bouwt de onderbouwing van de langere bewaartermijn juist op deze soort, en de gesloten lijst in punt 6 voert hem als eigen soort op. Op de kolom `onderwerp` was hij dus onvindbaar, en dat is precies waarom de zin hierboven op de scope telt. **Drie typen, niet twee.** Drie uit type 6 (`:175`, `:178` en `:179`, de `B`-rijen van paragraaf 2 van deel 1; de goedgekeurde en de afgewezen aanvraag dragen publiek `A` en vallen in type 7), twee uit type 12 (`:141` en `:301`) en een uit type 8 (`:278`). Dat spreekt deel 1 `:533` niet tegen, waar staat dat alleen de typen 6 en 12 "helemaal zonder publiek A" zijn: dat is een uitspraak over TYPEN, en type 8 heeft naast `:278` genoeg rijen met publiek `A`. Het is juist de reden dat de grendel op de SOORT hangt en niet op het type. Zonder deze tweede grendel zou deel 1 `:535` niet kloppen voor vijf van de zes: die zin luidt daar "type 12 is alleen zichtbaar voor platformbeheerders; type 6 alleen voor wie beoordeelt", en `:141` en `:301` vallen in type 12 en `:175`, `:178` en `:179` in type 6. De drie daarvan die vandaag bestaan zouden zonder de grendel op de projecttijdlijn staan en de projectwebhook uit deel 3, Kanaal 5 uit gaan, en de twee andere zodra ze bestaan. **De zesde staat niet in `:535` en hoort daar ook niet te staan.** `:278` valt in type 8, en dat type kan `:535` niet noemen, want het staat verder vol rijen met publiek `A`; wat deze ene rij buiten de projectkant houdt is uitsluitend zijn eigen publiek `B`. Daarmee is de grendel voor alle zes sluitend: vijf langs `:535` en de zesde langs zijn eigen publiek.

**De regel: een gebeurtenis komt alleen op de PROJECTKANT als hij een project draagt EN zijn publiek niet uitsluitend `B` is.** De projectkant is de projecttijdlijn en de projectwebhook; de platformtijdlijn heeft de grendel niet, want daar is `B` juist de reden dat je hem ziet. Dat spreekt de regel hierboven niet tegen, want die gaat over de SCOPE (een project of geen project) en deze over het PUBLIEK; een gebeurtenis die "een beetje" van een project is bestaat nog steeds niet, en deze zes zijn helemaal van hun project, alleen niet voor de mensen erin. Net als de bewaartermijn in punt 6 hangt dit aan de SOORT en niet aan de categorie: elke soort declareert bij de enum uit punt 7 zijn publiek, naast zijn ernst en naast de vraag of hij onder de langere bewaartermijn van punt 6 valt, en de tijdlijnbevraging en de bezorger lezen dat. Deel 1 `:535` tekent er zelf bij aan dat de aanvraag uit type 6 publiek `A` erbij krijgt zodra de eerste goedkeuring binnen een project komt te liggen; die soort valt dan vanzelf niet meer onder de grendel, en dat is de bedoeling van een grendel op het publiek en niet op het type.

**En een grendel op een KOLOM: `origin` is alleen voor platformbeheerders.** De netwerklocatie uit BIO2 8.15.01 is het enige veld in punt 7 dat niets over het project zegt en wel iets over waar een collega op dat moment was; hij staat er voor het incidentonderzoek uit 8.15.04, en dat is publiek `B`. Hij wordt dus wel vastgelegd maar niet getoond: niet op de projecttijdlijn, niet in het postvak, en niet in de body van de projectwebhook (deel 3, Kanaal 5, ontwerpregel 3). `actor` blijft daar wel staan. Punt 6 legt uit waarom die regel er juist met de langere bewaartermijn voor de beveiligingssoorten moet zijn: negen van de vijftien rijen die hem krijgen dragen publiek `A`, dus zonder deze regel zou het IP-adres van een collega jarenlang leesbaar zijn voor elke projectbeheerder.

**Twee leeswegen, twee regels, en dat is met opzet.** De TIJDLIJN van een project is de gebeurtenissentabel, gefilterd op projectnaam, achter `is_user_authorized_for_project`: hij volgt dus het HEDEN, en wie geen lid meer is ziet hem niet meer. Het POSTVAK is de ontvangertabel, gefilterd op ontvanger: het volgt het VERLEDEN, en wie geen lid meer is houdt wat er voor hem is neergelegd. Dat is geen inconsistentie maar het antwoord op de vraag uit richting C: de projectgeschiedenis is van het project, jouw meldingen zijn van jou. De uitzonderingen op de lidmaatschapsregel zijn de twee uit richting C (`:134`): de melding "je bent uit dit project verwijderd", die per ontvanger wordt neergelegd juist omdat de lidmaatschapsregel op dat moment niet meer geldt, en de platformmededeling, die per ontvanger wordt neergelegd omdat er helemaal geen lidmaatschap is om langs te vragen. Beide raken alleen het POSTVAK: de eerste staat op de tijdlijn van het project dat je verliet en die zie je niet meer, de tweede draagt geen project en staat dus op de platformtijdlijn.

De enige regel die hier een uitzondering nodig heeft is `cross-domain-access` (deel 1, paragraaf 10): daar is de belanghebbende een beheerder van een van TWEE projecten. In de ontvangertabel is dat geen probleem (er worden rijen voor beide gelegd, met een `reason` die zegt welke). In de tijdlijn is het dat wel, en het antwoord is dat zo'n gebeurtenis op beide tijdlijnen verschijnt door hem twee keer te schrijven, met verschillende `subject`. Dat is eerlijker dan een uitzondering in de bevraging.

**Deze paragraaf gaat over LEZEN, en dat is niet de hele vraag.** Er zijn meer wegen waarlangs gebeurtenissen naar buiten gaan zonder dat iemand ze opvraagt, en de zwaarste daarvan zijn de kanalen waarvan de BESTEMMING door een gebruiker wordt gekozen in plaats van een bekende ontvanger te zijn. Dat zijn er in dit plan twee: de webhook per project uit deel 3, Kanaal 5, en de Mattermost-kanaalwebhook uit deel 3, Kanaal 4, die nog niet is uitgewerkt maar dezelfde vorm heeft en dus dezelfde regels. Wie daar een abonnement mag neerzetten bepaalt WAARHEEN de gebeurtenissen van een heel project stromen, en dat is een zwaardere handeling dan lezen, niet een lichtere. Die regel hoort bij het kanaal en staat daarom in deel 3, Kanaal 5, ontwerpregel 5, geformuleerd op de klasse en niet op dat ene kanaal: aanmaken is een handeling van een projectbeheerder met een identiteit en niet iets dat op de projectsleutel kan. Waar de aanvraag heen mag is ontwerpregel 4 daarnaast.

### Wat er in een gebeurtenis terechtkomt

Dit is de scherpste rand van het hele ontwerp. Een `error_message` uit een connector kan een geheim dragen, en de weg daarheen is kort.

Twee gemeten precedenten in deze repo. `opi/utils/api_keys.py:39` logt de volledige API-sleutel op DEBUG als `USE_UNSAFE_API_KEY` aanstaat; die vlag staat op productie uit (`bootstrap/rig-system/kustomize/operations-manager/overlays/odcn-production/configmap.yaml:43`, `USE_UNSAFE_API_KEY=false`), maar de `opi`-logger staat onvoorwaardelijk op DEBUG (`opi/utils/logging_config.py`), dus een omgevingsvariabele scheidt dat van een sleutel in de log (bevinding G in `plans/technische-review-bio-en-nora-bevindingen.md`, met BIO2 8.15.02 erbij: een logregel bevat nooit gegevens die tot het doorbreken van de beveiliging kunnen leiden). En de mailconnector, de Keycloak-connector en de postgres-connector krijgen allemaal wachtwoorden mee die in een uitzonderingsboodschap kunnen belanden.

**Aanbeveling: een gebeurtenis bouwt zijn eigen velden op, en neemt nooit een vrije `str(exception)` over.** Concreet: de gebeurtenissoort bepaalt welke velden er in `data` staan, en een foutmelding komt er alleen in als de code hem expliciet heeft samengesteld. Dat is strenger dan wat `async_tasks.error_message` vandaag doet, en met opzet: die tabel wordt na een uur geleegd en een gebeurtenissenlog niet.

**Aanbeveling: een redactiefunctie op de schrijfweg, niet op de leesweg.** Redactie bij het tonen is geen redactie: de waarde staat dan al in de database, in de backup, en in elke export. Er is vandaag precies een redactiehulpmiddel, en het is te smal om hier op te leunen: `redact_sensitive_headers` in `opi/utils/logging_redact.py:25` maskeert zeven HTTP-headernamen (`authorization`, `x-api-key`, `cookie` en vier andere) en wordt op een plek aangeroepen, in `opi/connectors/argo.py`. Er is geen redactie voor waarden binnen een foutmelding. Dat hulpmiddel moet er dus komen; het bestaande is er het beginpunt van, niet de oplossing.

Deze regel valt samen met regel 2 uit de CloudEvents-beslechting hierboven en met de grens voor wat er in een mail mag (deel 3, Kanaal 3). Dat de drie samenvallen is geen toeval: het is drie keer dezelfde vraag, namelijk wat er buiten de kortste bewaartermijn en buiten de nauwste kring terechtkomt.

## 5. De verhouding tot het audittrail

**Kort antwoord: twee dingen, en ze mogen best in een tabel beginnen.**

De vraag is of "wat is er gebeurd" (bewijs, onveranderlijk, compleet) en "wat moet jij weten" (persoonlijk, wegklikbaar) hetzelfde zijn. In het aanbevolen model zijn ze dat al **niet**, want het zijn twee tabellen: de gebeurtenis is onveranderlijk en compleet, de ontvangerrij is persoonlijk en wegklikbaar. Wat de gebruiker wegklikt is zijn rij, niet de gebeurtenis. Dat is ook de reden dat punt 3 de teller naar de ontvangerrij verplaatst: de gebeurtenissentabel moet alleen-invoegen blijven om deze rol te kunnen spelen.

**Wat er vandaag aan audittrail ligt**, want dat is minder dan je zou denken:

| Wat | Waar | Vorm |
|---|---|---|
| Wijzigingen aan een projectbestand | de git-geschiedenis van `zad-projects` | commits, compleet, onveranderlijk, maar onder een vaste systeemidentiteit (deel 1) |
| Goedkeuringsoordelen | het `history`-blok in het projectbestand | wie, wanneer, welk oordeel, welke notitie |
| Automatische stemming | het `history`-blok bij `resources` in het projectbestand | tijdstip, bron, reden, geen actor |
| Domeinclaims | de logger `opi.audit.subdomain` (`subdomain_registry.py:211` e.v.) | logregels, dus zo lang als de logretentie |
| Runs | de tabel `runs`, met `started_by`, `started_at`, `ended_by` | rijen, niet opgeruimd |
| Taken | de tabel `async_tasks` | rijen, **na een uur weg** |
| Inloggen en uitloggen | Keycloak-auditevents, 90 dagen | alleen op realms die na 20 juli 2026 zijn aangemaakt; zie deel 1 |

Er is dus **geen audittabel**. `plans/bio2-compliance-analysis.md` benoemt dat zelf twee keer, met twee verschillende zwaarten: onder A8.15 als HIGH ("Logging exists but no structured audit trail (who did what, when)", `:55`) en onder A5.28 als MEDIUM ("No forensic logging or tamper-proof audit trail", `:48`). `plans/technische-review-bio-en-nora-bevindingen.md` komt bij bevinding E op dezelfde conclusie langs een andere weg.

**De BIO-kant, kort en concreet.** De relevante controls:

- **A8.15 (logging)**: een gebeurtenissentabel met wie, wat, wanneer en waarover is precies wat daar ontbreekt. Gebeurtenissen leveren dat als bijvangst, mits de gebeurtenis de actor draagt.
- **A8.16 (monitoring)**: het gaat over waarnemen, en dat doet Prometheus. Gebeurtenissen raken dit niet; de metriekhelft wel (deel 3, Kanaal 6).
- **A5.24 (incidentbeheer)**: nu genoteerd als "errors logged but no escalation/notification". Dit is letterlijk wat hier gebouwd wordt.
- **A5.28 (bewijs)**: vraagt om onveranderlijkheid. Dat is een ontwerpregel voor de gebeurtenissentabel: alleen invoegen, nooit bijwerken, nooit verwijderen binnen de bewaartermijn. De pseudonimisering uit punt 6 is de enige toegestane wijziging, en die is er een die verwijdert en niet toevoegt.

**De aanbeveling voor het audittrail zelf: doe het niet als eigen opdracht, maar bouw de gebeurtenissentabel zo dat hij het kan worden.** Concreet: elke gebeurtenis draagt de actor, het tijdstip en het onderwerp; de tabel is alleen-invoegen; de bewaartermijn van de gebeurtenissen is langer dan die van de ontvangerrijen. Dan is "maak er een audittrail van" later een kwestie van meer gebeurtenissen aanleggen en een langere bewaartermijn, en niet van een tweede tabel.

## 6. Bewaartermijn en persoonsgegevens

Een gebeurtenissenlog met e-mailadressen erin is een verwerking van persoonsgegevens. De actor is een medewerker, het e-mailadres identificeert hem, en het verslag zegt wat hij deed en wanneer. Dat is geen randgeval.

### Geldt het Logboek Dataverwerkingen hier

**Nee, en dat is een inhoudelijk antwoord, geen ontsnapping.** De standaard (werkversie, nog geen vastgestelde versie, nog niet op de lijst van het Forum Standaardisatie) is gebouwd rond twee verplichte attributen: `dpl.core.processing_activity_id`, een verwijzing naar een verwerkingsactiviteit in het register uit AVG artikel 30, en `dpl.core.data_subject_id` met `data_subject_id_type` (`BSN`, personeelsnummer, URI), de betrokkene op wiens gegevens de verwerking betrekking heeft. Het doel is transparantie richting de burger: welke organisatie raakte wanneer mijn gegevens aan, over organisatiegrenzen heen te volgen via W3C Trace Context.

ZAD verwerkt geen burgergegevens. Een gebeurtenis in ZAD zegt "deze beheerder heeft de geheugengrens van dit component verhoogd". De betrokkene, als je die al wilt aanwijzen, is de handelende medewerker zelf, en dan valt `data_subject_id` samen met `actor`, wat de standaard niet bedoelt. Er is geen verwerkingsactiviteit uit een artikel-30-register om naar te verwijzen, want dit is geen verwerking van persoonsgegevens als doel maar als bijvangst van beheerhandelingen.

**Dit moet expliciet opgeschreven staan, want anders wordt het per ongeluk toch gebouwd.** Wat wel overneembaar is, en de moeite waard: de **vorm**. Een gebeurtenis met een correlatie-identificatie die over systeemgrenzen meereist is precies wat `flow_id` (`opi/core/flow_id.py`) vandaag al doet binnen een proces, en OTLP als exportprotocol is een verstandige keuze om andere redenen dan naleving. NEN 7513 is een zorg-specifieke uitbreiding op deze standaard en is hier niet van toepassing.

*Voorbehoud:* het bovenstaande is gebaseerd op de skill `standaarden:ls-logboek` (versie 0.3.9, met een eigen conceptvoorbehoud) en niet op de normatieve tekst bij Logius. Als iemand een dwingende reden heeft om de standaard wel toe te passen, is dat een gesprek over de scope van het artikel-30-register en niet over de techniek.

### Geldt de BIO2 hier

**Ja, en er ligt al een uitspraak in dit project die het aanscherpt.** `features/bio-network-access-no-vpn-compliance.md` legt vast dat ZAD bewust geen VPN gebruikt en dat dat onder BIO2 v1.3 verdedigbaar is, mits onder meer "segmentatie, sterke authenticatie en logging/monitoring de functie van een VPN compenseren". In de tabel met compenserende maatregelen staat letterlijk: "Detectie/herleidbaarheid: Logging (8.15) + monitoring (8.16)".

Dat is een claim die vandaag zwakker staat dan het document suggereert. De logs zijn niet in ons beheer, de enige tabel met een actor wordt na een uur geleegd, en de git-commits dragen een vaste systeemidentiteit.

De drie overheidsmaatregelen die hier gelden, letterlijk:

| Maatregel | Tekst | Wat dat hier betekent |
|---|---|---|
| **8.15.01** | Een logregel bevat minimaal: Actie (de gebeurtenis of handeling die heeft plaatsgevonden), Object (waarop de gebeurtenis of handeling effect had), Resultaat, Oorsprong (het apparaat of de netwerklocatie van waaruit de handeling in gang is gezet), Actor (identificatie van de persoon die of het proces dat de gebeurtenis in gang heeft gezet) en Tijdstempel | De zes velden zijn de kolommen `type`, `subject`, `severity` plus `data`, `origin`, `actor` en `time` uit punt 7, met een uitzondering op Actor die punt 7 zelf neerzet: op de wegen die geen identiteit dragen (de projectsleutel, een scheduler, het cluster) blijft `actor` NULL en wordt de Actor-eis gedragen door `actor_kind` plus `project` plus `origin` samen, want 8.15.01 vraagt de persoon OF het proces. Een-op-een zijn de zes dus alleen op de wegen met een identiteit. Precies de actor ontbreekt vandaag; dat is bevinding E in `plans/technische-review-bio-en-nora-bevindingen.md`. Merk op dat **Oorsprong** een veld is dat geen van beide bronstukken had: het IP-adres of de netwerklocatie. Vandaag draagt geen enkele logregel bij een geweigerde API-sleutel dat gegeven (deel 1, groep 7), dus dit is een echte toevoeging aan het datamodel en niet alleen een hernoeming |
| **8.15.02** | Een logregel bevat nooit gegevens die tot het doorbreken van de beveiliging kunnen leiden | Dit is de grondslag onder de redactie-eis in punt 4, en het is de reden dat redactie op de schrijfweg hoort en niet op de leesweg |
| **8.15.04** | De bewaartermijn van logbestanden en gegevens in het Security Incident en Event Monitoring (SIEM) worden risicogericht bepaald, rekening houdend met het scenario dat aanvallers langdurig binnen zijn | Een uur is dat niet. En het is de grondslag onder de aparte, langere termijn voor beveiligingsgebeurtenissen hieronder |

Met andere woorden: het gebeurtenissenwerk is niet alleen een gebruikerswens. Het is de compenserende maatregel die in een bestaande risicoafweging al is opgeschreven maar nog niet is waargemaakt.

*Herkomst van deze drie teksten.* Een eerdere versie van dit document tekende bij deze uitspraken aan dat ze niet onafhankelijk waren geverifieerd, omdat ze uit een ander document in deze repo kwamen en niet uit de normatieve bron. Dat voorbehoud is vervallen: de drie teksten hierboven zijn op 28 augustus 2026 nagelezen in de publicatie van BZK zelf, `MinBZK/Baseline-Informatiebeveiliging-Overheid`, deel 2 (BIO-overheidsmaatregelen) van BIO2 v1.3, en komen daar woordelijk zo voor. Dat is een sterkere bron dan de citaten in deze repo en sterker dan een samenvatting. Twee dingen horen erbij. Ten eerste draagt die GitHub-publicatie zelf een disclaimer: "De BIO2 versie 1.3 in de GitHub-omgeving heeft geen formele status. De inhoud van dit document kan afwijken van de formele documentatie. De officiele versie van de BIO2 is beschikbaar via de BIO-website." Wie hierop een Verklaring van Toepasselijkheid bouwt, leest bio-overheid.nl. Ten tweede: de citaten in deze repo klopten, alle drie, woord voor woord op de kern. Ze staan alle drie in `plans/technische-review-bio-en-nora-bevindingen.md`, op twee plekken: 8.15.01 en 8.15.04 bij bevinding E (`:99`), 8.15.02 bij bevinding G (`:113`). Een grep over de hele repo op de maatregelnummers geeft geen andere vindplaats, dus er is geen tweede document dat deze teksten citeert.

Twee maatregelen uit dezelfde reeks kwamen er bij het nalezen bij, en ze raken dit ontwerp direct, dus ze horen genoemd:

- **8.15.05**: oneigenlijk wijzigen of verwijderen van loggegevens, of pogingen daartoe, worden zo snel mogelijk gemeld als informatiebeveiligingsincident. Dat is een argument te meer voor alleen-invoegen, en het is zelf een gebeurtenissoort.
- **8.15.06**: op basis van een expliciete risicoafweging bepaalt de entiteit de periodieke toetsing op het ongewijzigd bestaan van logbestanden gedurende de bewaartermijn, uitgevoerd door een onafhankelijke functionaris. Dat is geen bouwwerk in dit traject, maar het is wel de reden dat de tabel geen `UPDATE`-pad hoort te hebben behalve de pseudonimisering.

### Aanbeveling voor de bewaartermijn

De twee bronstukken kwamen hier op verschillende getallen uit: het ene op een jaar voor de gebeurtenis, het andere op 90 dagen met pseudonimisering daarna. **Beslecht: het zijn twee verschillende klokken op dezelfde rij, en ze gelden allebei.**

**Na 90 dagen worden de actor en de oorsprong gepseudonimiseerd; de rij blijft staan tot een jaar.** Negentig dagen voor het persoonsgegeven, omdat die termijn hier al een keer is gekozen en verdedigd, namelijk voor de Keycloak-auditevents (`eventsExpiration: 7776000`, in zes realm-configuraties); dezelfde termijn twee keer gebruiken is makkelijker uit te leggen dan een nieuw getal verzinnen. Wat er na die 90 dagen overblijft is "wat is er met dit project gebeurd", en dat is een beheergeschiedenis en geen persoonsgegeven; wat verdwijnt is "wie deed het en waarvandaan", en dat zijn de persoonsgegevens. Het gaat dus om twee kolommen en niet om een: `actor` en `origin`, want de netwerklocatie uit 8.15.01 is er net zo goed een. Een jaar voor de rij zelf, omdat de tabel de vraag "wat is er in het laatste kwartaal gebeurd" moet kunnen beantwoorden en omdat hij later een audittrail kan worden.

Dat lost ook het bezwaar op dat de twee getallen elk apart hadden. Een jaar met de actor erin is een jaar persoonsgegevens bewaren zonder grondslag. Negentig dagen met verwijdering van de rij gooit de beheergeschiedenis weg die je juist wilde houden.

**Wat "pseudonimiseren" hier betekent, want dat woord doet veel werk en het is nergens in de bronstukken uitgelegd.** **De lus zet `actor` en `origin` allebei op `NULL`.** Niet op een plaatshouder-tekst zoals `verwijderd`, en dat is hier geen smaakkwestie maar een eis van de index: `idx_notification_events_actor_sweep` (punt 7) is partieel op `actor IS NOT NULL OR origin IS NOT NULL`, dus een rij die na de lus nog een tekst in `actor` draagt valt daar nooit meer uit, en dan groeit die index monotoon in plaats van te krimpen zoals punt 7 belooft. Met `NULL` valt een verwerkte rij er vanzelf uit, en dat is precies wat de onderbouwing hieronder gebruikt. Uitdrukkelijk ook NIET met een stabiele hash of een sleutelgebonden pseudoniem per persoon: dat zou de rijen van dezelfde persoon aan elkaar geknoopt houden, en bij een populatie van een handvol platformbeheerders is zo'n stabiel pseudoniem met wat puzzelwerk weer een naam. Wat er overblijft is dus onomkeerbaar, en strikt genomen is dat anonimiseren en geen pseudonimiseren in de zin van AVG artikel 4 lid 5; het woord komt uit de bronstukken en blijft hier staan omdat het inmiddels in alle drie de documenten wordt gebruikt, maar dit is wat de lus doet. Dat is ook waarom punt 5 hierboven de pseudonimisering onder A5.28 een wijziging mag noemen die verwijdert en niet toevoegt. `actor_kind` blijft wel staan, want "mens", "agent" of "scheduler" wijst niemand aan; daaraan ziet een scherm ook dat er een persoon is weggehaald in plaats van dat er nooit een was, dus daar is geen plaatshouder-tekst voor nodig. Bij `agent` is die aflezing niet sluitend, en dat is bewust aanvaard: een lege `actor` betekent daar of de sleutelweg, waar er nooit een persoon was (punt 7), of een gepseudonimiseerde bearer-tokenweg, waar er wel een was. In allebei de gevallen is er niemand meer aan te wijzen, en dat is precies wat het scherm hoort te tonen; wie het onderscheid toch nodig heeft, heeft de gebeurtenis nodig voordat de termijn verstreek en niet een extra kolom. En dat het bij die twee kolommen kan blijven is geen aanname maar een eis: punt 4 schrijft voor dat een gebeurtenis zijn eigen velden opbouwt, dus er hoort geen e-mailadres in `data` of in `summary` te staan. Komt het daar toch in, dan is dat een fout in die soort en niet een reden om de lus uit te breiden.

**Beveiligingsgebeurtenissen: langer, met de actor erin, en dat is een aparte beslissing.** Een geweigerde API-sleutel, een geweigerd bearer-token of een geblokkeerde allowlist-controle is precies het spoor dat je bij een incident maanden later terug wilt lezen; 8.15.04 noemt langdurig aanwezige aanvallers met zoveel woorden. Wat "langer" is, is een beslissing voor een mens (deel 3), en 90 dagen is daarvoor waarschijnlijk aan de korte kant.

**En dat zijn niet alleen de geweigerde pogingen, want een aanval laat meestal juist geen weigering achter.** Wie een beheerdersessie overneemt en zichzelf een projectrol toekent krijgt overal een 200: er wordt niets geweigerd, er komt geen allowlist aan te pas, en de enige regel die er ooit van overblijft is de GESLAAGDE rechtenwijziging. Staat die op de gewone klok, dan is op dag 91 "wie en waarvandaan" weg terwijl de rij zelf nog een driekwart jaar blijft staan, en er is niets dat dat opvangt: de Keycloak-auditevents lopen op precies dezelfde 90 dagen (`eventsExpiration: 7776000`, en dat is hierboven juist de reden voor dat getal) en de git-historie van `zad-projects` staat onder een vaste systeemidentiteit (`opi/connectors/git.py:53`, zie deel 1). Dat is het scenario van 8.15.04 zelf. De langere termijn moet daarom de rechten- en accountwijzigingen meenemen, en niet alleen de afgeslagen pogingen.

**Beslecht: die uitzondering hangt aan de SOORT (`type`) en niet aan de categorie (`category`).** De twee bronstukken zeiden hier allebei te weinig om het verschil te zien: het ene gaf een termijn zonder enige categorie, het andere schreef "beveiligingsgebeurtenissen: langer" zonder erbij te zeggen waaraan je die herkent. Bij het samenvoegen is dat een tijdlang categorie 12 uit deel 1 (`beheer-en-beveiliging`) geweest, en die is tegelijk te grof en te smal. **Te grof**, want deel 1 wijst drieentwintig catalogusrijen aan dat type toe (het telt ze af onder de typentabel) en de meeste daarvan zijn gewoon beheer: een subdomein is geclaimd (deel 1 `:302`), een certificaat verloopt binnenkort (deel 1 `:308`), de reconciliatie sloeg een ronde over (deel 1 `:232`). Die de actor onbeperkt laten houden is precies wat de alinea hierboven verbiedt, want een jaar met de actor erin is een jaar persoonsgegevens bewaren zonder grondslag, en bij "een subdomein is geclaimd" is die grondslag er niet. **Te smal**, want de rechtenwijzigingen die het aanvalsscenario hierboven draagt zitten helemaal niet in type 12: een lid erbij, een rol gewijzigd, een lid eruit en een projectrol toegekend vallen in type 8 (`leden-en-toegang`), en "een project heeft toegang tot een ander project gekregen of verloren" (deel 1 `:379`) net zo goed. Punt 3 legt het onderscheid trouwens al vast: soort en categorie zijn twee dingen, en alleen de categorie staat in het voorkeurenscherm. De bewaartermijn hoort bij de soort, net als de ernst.

**Het criterium: een gebeurtenis valt onder de langere termijn als hij een RECHT, een ACCOUNT of een TOEGANGSPOGING betreft.** Drie woorden, en ze staan hier zodat een soort die later bijkomt er vanzelf onder valt: wie een nieuwe soort declareert toetst hem aan deze drie en niet aan de lijst hieronder.

- **Een recht**: iemand of iets krijgt, houdt of verliest toegang tot een project, een rol of een bevoegdheid. Een uitnodiging is een uitgedeeld recht, een inwisseling is het opnemen ervan, en een ledenwijziging is het recht zelf.
- **Een account**: een account op het platform wordt aangemaakt, gewijzigd of verwijderd.
- **Een toegangspoging**: een poging binnen te komen of iets te gebruiken, geslaagd of geweigerd.

De grondslag is bij alle drie dezelfde en het is dezelfde als bij de weigeringen: de verantwoording uit BIO2 8.15 hierboven, met 8.15.04 als de maatregel die om de langere termijn vraagt. Wie zegt dat een beheerhandeling op een account of een recht die grondslag niet heeft, zegt tegelijk dat de weigering hem wel heeft, en dat verschil is niet te verdedigen: het is dezelfde verantwoording over dezelfde toegang, alleen aan de andere kant van de uitkomst.

**De lijst is gesloten en staat in code**, naast de enum waar `type` uit komt (punt 7). Vandaag zestien soorten over vijftien catalogusrijen (de sleutel en het token delen alleen in de catalogus een rij), en ze liggen in twee verschillende typen, wat precies is waarom de termijn aan de soort hangt en niet aan de categorie. Hierna heten ze de **beveiligingssoorten**, en dat woord betekent vanaf hier de hele lijst en niet alleen de geweigerde pogingen waarmee hij begon. VOORSTEL voor de namen:

| Soort (VOORSTEL) | Waarom | Catalogusrij | Waar hij ontstaat |
|---|---|---|---|
| `nl.rig.zad.beveiliging.allowlist-geweigerd.v1` | toegangspoging | deel 1 `:295` | `opi/middleware/authorization.py:117` |
| `nl.rig.zad.beveiliging.sleutel-geweigerd.v1` | toegangspoging | deel 1 `:296` | `opi/api/endpoint_util.py:52`, `:69`, `:135`, `:176` |
| `nl.rig.zad.beveiliging.token-geweigerd.v1` | toegangspoging | deel 1 `:296` | `opi/api/user_token_auth.py:252` |
| `nl.rig.zad.beveiliging.inwisseling-geweigerd.v1` | toegangspoging | deel 1 `:273` | `opi/manager/invite_manager.py:73`, `:101` |
| `nl.rig.zad.beveiliging.uitnodigingscode-ongeldig.v1` | toegangspoging | deel 1 `:274` | `opi/manager/invite_manager.py:128` |
| `nl.rig.zad.beveiliging.account-aangemaakt.v1` | account | deel 1 `:292` | `opi/services/user_admin_service.py:41` |
| `nl.rig.zad.beveiliging.account-gewijzigd.v1` | account | deel 1 `:293` | `opi/services/user_admin_service.py:51` |
| `nl.rig.zad.beveiliging.account-verwijderd.v1` | account | deel 1 `:294` | `opi/services/user_admin_service.py:65` |
| `nl.rig.zad.beveiliging.uitnodiging-aangemaakt.v1` | recht | deel 1 `:270` | `opi/api/invite_routes.py` |
| `nl.rig.zad.beveiliging.uitnodiging-ingewisseld-sso.v1` | recht | deel 1 `:271` | `opi/manager/invite_manager.py:262` |
| `nl.rig.zad.beveiliging.uitnodiging-ingewisseld-lokaal.v1` | recht | deel 1 `:272` | `opi/manager/invite_manager.py:355` |
| `nl.rig.zad.beveiliging.projectrol-toegekend.v1` | recht | deel 1 `:278` | `opi/manager/invite_manager.py:187`, `:153` |
| `nl.rig.zad.beveiliging.lid-toegevoegd.v1` | recht | deel 1 `:275` | `opi/web/router_detail_edit.py`, de vergelijking oud-nieuw uit punt 1 |
| `nl.rig.zad.beveiliging.rol-gewijzigd.v1` | recht | deel 1 `:276` | idem |
| `nl.rig.zad.beveiliging.lid-verwijderd.v1` | recht | deel 1 `:277` | idem |
| `nl.rig.zad.beveiliging.projecttoegang-gewijzigd.v1` | recht | deel 1 `:379` | `opi/services/catalog/cross_domain_access/` |

De schrijfwegen liggen niet allemaal in dezelfde fase van deel 3, en dat hoeft ook niet: de drie ledenwijzigingen worden in fase 3 aangelegd en krijgen hun actor in fase 4, de account-, uitnodigings- en toegangswegen komen in fase 4, en `nl.rig.zad.beveiliging.projecttoegang-gewijzigd.v1` hangt aan de dienstwijzigingen, die geen eigen fase hebben. Wat wel voor alle zestien geldt: wie de soort aanlegt, legt hem aan met `actor` en `origin` gevuld, want anders draagt de langere termijn niets.

**De keerzijde van de verbreding, en die is met de verbreding een andere geworden.** Zolang de langere termijn alleen de geweigerde pogingen dekte, raakte hij uitsluitend rijen met publiek B: die staan op geen enkele projecttijdlijn en niemand buiten de platformbeheerders ziet ze ooit. Met het criterium erbij is dat niet meer zo. Negen van de vijftien catalogusrijen achter deze zestien soorten dragen publiek A: de vijf uitnodigingsrijen (deel 1 `:270`, `:271`, `:272`, `:273` en `:274`, waarvan `:273` `A en B`), de drie ledenwijzigingen (`:275`, `:276`, `:277`) en de projecttoegang (`:379`, `A en B`); alleen de andere zes (`:278`, `:292` tot en met `:294`, `:295` en `:296`) zijn B. Die negen staan dus op de PROJECTtijdlijn, en lezen loopt daar volgens punt 4 langs `is_user_authorized_for_project` en verder nergens langs. Zonder een regel erbij betekent de langere termijn daarmee dat het e-mailadres en het IP-adres van een collega jarenlang leesbaar blijven voor elke projectbeheerder, en de afweging hierboven ("een jaar met de actor erin is een jaar persoonsgegevens bewaren zonder grondslag") is gemaakt toen de uitzondering alleen platformrijen raakte. Die afweging is hier opnieuw gemaakt, en zij valt voor de twee kolommen verschillend uit.

- **`actor` blijft zichtbaar op de projecttijdlijn, en dat is bewust aanvaard.** Wie een lid toevoegde, een rol wijzigde of een uitnodiging aanmaakte is een beheerder van datzelfde project, en de betrokkene staat sowieso met zijn e-mailadres in het projectbestand dat diezelfde beheerders lezen. Dit is precies de verantwoording waarvoor de tijdlijn er is: "wie heeft mij eruit gezet" onbeantwoordbaar maken voor de enige mensen die er iets mee moeten is geen gegevensbescherming maar het weghalen van de waarde. De verwerking blijft bovendien begrensd, want ook deze soorten hebben een termijn (deel 3, openstaande beslissing 7).
- **`origin` niet: die is alleen zichtbaar voor platformbeheerders.** Van alle kolommen in punt 7 is de netwerklocatie de enige die niets over het project zegt en wel iets over waar een collega op dat moment was, en niemand heeft hem nodig om een project te beheren; hij staat er voor het incidentonderzoek uit 8.15.04, en dat is publiek B. Dat is een LEESregel en geen schrijfregel: `origin` wordt gewoon vastgelegd, want anders draagt de langere termijn niets. Punt 4 legt hem vast en zegt waar hij overal geldt.

Drie soorten horen erbij zodra ze bestaan, en ze staan hier zodat ze niet vergeten worden. Het inloggen en uitloggen (deel 1 `:297`) zodra ZAD dat zelf vastlegt in plaats van te leunen op de Keycloak-auditevents, die vandaag hun eigen klok van 90 dagen hebben (`eventsExpiration: 7776000`). "Iemand is platformbeheerder geworden of afgevoerd" (deel 1 `:303`), dat naar het criterium de zwaarste rechtenwijziging is die dit platform kent maar vandaag geen schrijfweg heeft, want de allowlist komt uit de configuratie en niet uit een handeling in de applicatie; wie die handeling ooit naar binnen haalt, haalt deze soort mee. En het oneigenlijk wijzigen of verwijderen van loggegevens uit 8.15.05, dat hierboven al een gebeurtenissoort wordt genoemd.

**Wat er NIET bij hoort.** De rest van type 12, want dat is beheer zonder recht: een subdomein is geclaimd (deel 1 `:302`), een certificaat verloopt binnenkort (deel 1 `:308`), de bootstrap-drift, de reconciliatie sloeg een ronde over (deel 1 `:232`), de takenmachinerie van OPI. En ook de scanbevinding niet (deel 1 `:306`): een scanuitslag is geen aanvalsspoor en draagt geen actor. Van de drieentwintig catalogusrijen van type 12 vallen er vijf onder het criterium en komen er twee bij zodra ze bestaan; de zestien andere volgen de gewone klok.

**Welke kolom de lus dan leest: `type`, en verder verandert er niets.** De lus heeft twee passes en alleen de EERSTE draait op `idx_notification_events_actor_sweep` (punt 7): de pseudonimiseringspas, die na 90 dagen `actor` en `origin` leegmaakt, leest die partiele index op `time` over de rijen die nog een persoonsgegeven dragen, en omdat de pas in allebei die kolommen `NULL` schrijft en geen plaatshouder-tekst, valt elke rij die hij verwerkt daarna uit de index, en dat is wat het handjevol een handjevol houdt. De TWEEDE pas, die de rij na een jaar opruimt, kan die index per definitie niet gebruiken: zijn rijen zijn juist de rijen die er al uit zijn gevallen, en een gebeurtenis van een scheduler stond er sowieso nooit in, want hij draagt geen van beide kolommen. Dat is geen aanname maar volgt uit het model in punt 7: `actor` draagt een e-mailadres en een scheduler heeft er geen, dus daar staat `NULL`, en `origin` is leeg omdat er geen netwerklocatie is. Dat het een scheduler was staat in `actor_kind`, en die kolom valt buiten dit predicaat. Waar deel 3 schrijft "bij een scheduler is de actor de scheduler" gaat dat dus over `actor_kind` en niet over `actor`. Een gebeurtenis van een agent die alleen de projectsleutel aanbiedt draagt evenmin een `actor`, en om een reden die punt 7 uitschrijft: die sleutel identificeert het project en niet de handelende partij. Maar hij is daarmee GEEN geval als de scheduler, want hij draagt wel een `origin`, en dus staat hij wel degelijk in de sweep-index en is hij juist een rij die de eerste pas verwerkt. Die pas loopt daarom op `time` en op niets anders, en daarvoor staat `idx_notification_events_retention` in punt 7, in dezelfde vorm als `idx_async_tasks_completed` bij het patroon waar dit van erft (`opi/services/persistence/async_tasks.py:63-68`). De uitzondering is een `type <> ALL (:beveiligingssoorten)` in dezelfde `WHERE`, en zij geldt voor allebei de passes. Zij vraagt zelf geen index en geen kolom erbij: de lijst is een constante van zestien waarden en het filter valt op het handjevol rijen dat de index per dag overhoudt. Een `retention_class`-kolom zou hetzelfde doen met een schemawijziging erbij, en die is pas nodig als de termijn per geval gaat verschillen in plaats van per soort.

| Tabel | Voorstel | Waarom |
|---|---|---|
| `notification_events`, de gewone soorten | **`actor` en `origin` gepseudonimiseerd na 90 dagen, rij verwijderd na 1 jaar** | het persoonsgegeven volgt de Keycloak-termijn; de beheergeschiedenis blijft nog een driekwart jaar bevraagbaar |
| `notification_events`, de beveiligingssoorten hierboven | **`actor` en `origin` blijven, termijn is een beslissing van een mens** | 8.15.04 vraagt een risicogerichte termijn; 90 dagen is voor een aanvalsspoor waarschijnlijk te kort |
| `notification_deliveries` | **90 dagen na lezen; ongelezen blijven staan zolang de gebeurtenis bestaat** | een melding die je nooit las mag niet verdwijnen; een gelezen melding is klaar |
| `notification_channel_deliveries` | **30 dagen na `sent_at`** | dit is uitsluitend werkadministratie |
| `notification_preferences` | **niet opruimen**, wel mee met de gebruiker | een voorkeur is een instelling |

**Wie hem opruimt: geen nieuwe planner.** Een tweede lus in de outboxplanner, in de vorm van `cleanup_old_tasks` (`opi/core/async_task_service.py:670`, aangeroepen uit `task_worker.py:420`) en van de retentiesweep die de backupplanner een keer per dag draait (`backup_scheduler.py:195`). Een keer per dag, na kantooruren, en de getallen instelbaar via `settings` met dezelfde naamgeving als de rest (`NOTIFICATIONS_EVENT_RETENTION_DAYS`, VOORSTEL).

**Bij het verwijderen van een gebruiker**: zijn `notification_deliveries`, `notification_channel_deliveries` en `notification_preferences` gaan mee. De `notification_events` blijven, want die gaan over het platform; wel worden zijn `actor` en `origin` daar meteen gepseudonimiseerd in plaats van pas na 90 dagen, **met dezelfde uitzondering als bij de klok hierboven: op de zestien soorten uit het criterium blijven ze staan.** Zonder die uitzondering is je account laten opheffen de goedkoopste manier om je eigen aanvalsspoor te wissen, en dat is precies het scenario waar 8.15.04 op doelt. Merk op dat het criterium juist hier het verschil maakt: was de lijst beperkt gebleven tot de geweigerde pogingen, dan zou een opheffing de actor wissen van elke rechten- en accountwijziging die deze persoon ooit deed, en dat is de helft van het spoor die er bij een overgenomen sessie als enige is. Met de uitzondering blijft de verwerking begrensd, want ook die soorten hebben een termijn. Dat hoort in `UserAdminService.delete_user` (`opi/services/user_admin_service.py:65`) en het is een regel die nu opgeschreven moet worden, want anders wordt hij vergeten.

**Het afmeldpad hoort goedkoop te zijn.** Iemand die geen mail meer wil, moet dat kunnen zonder dat er een deployment op gang komt. Dat is het praktische argument achter de keuze om abonnementen en voorkeuren in de database te zetten, en het is tegelijk een AVG-argument: een bezwaarrecht dat een commit veroorzaakt, is een bezwaarrecht met een drempel.

## 7. Het datamodel

In de stijl die er ligt: SQLAlchemy-modellen onder `opi/services/persistence/`, op de `Base` uit `opi/core/db.py`, geregistreerd in `opi/services/persistence/__init__.py`, en een migratie onder `opi/migrations/versions/`.

**Alle namen hieronder zijn een VOORSTEL.** Ze volgen de beslechting bovenaan dit document: Engels en meervoud zoals `async_tasks`, `runs`, `users`, `marked_for_deletion` en `subdomain_registry`, met de zes namen die het NL GOV-profiel voorschrijft ongewijzigd overgenomen.

### `notification_events` (VOORSTEL): de gebeurtenis

Onveranderlijk. Alleen invoegen. De enige toegestane wijziging na het schrijven is de pseudonimisering van `actor` en `origin` uit punt 6.

```python
class NotificationEvent(Base):
    """Een gebeurtenis op het platform. Onveranderlijk: alleen invoegen."""

    __tablename__ = "notification_events"

    # CloudEvents: id, source, specversion, type, subject, time
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, server_default=text("gen_random_uuid()"))
    source: Mapped[str] = mapped_column(String(255), nullable=False)      # urn:nld:oin:<OIN>:systeem:zad-<cluster>
    specversion: Mapped[str] = mapped_column(String(8), nullable=False, server_default=text("'1.0'"))
    type: Mapped[str] = mapped_column(String(128), nullable=False)        # nl.rig.zad.deployment.mislukt.v1
    subject: Mapped[str | None] = mapped_column(String(255))              # <project> of <project>/<deployment>
    time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )

    # Van ons, want het profiel zegt er niets over
    category: Mapped[str] = mapped_column(String(32), nullable=False)     # een van de twaalf typen uit deel 1
    severity: Mapped[str] = mapped_column(String(16), nullable=False)     # informational|actionable|outage
    cluster: Mapped[str] = mapped_column(String(63), nullable=False)
    project: Mapped[str | None] = mapped_column(String(63))
    deployment: Mapped[str | None] = mapped_column(String(63))
    component: Mapped[str | None] = mapped_column(String(63))
    actor: Mapped[str | None] = mapped_column(String(255))                # e-mailadres; NULL waar de WEG geen identiteit draagt: de projectsleutel, en de wegen achter `actor_kind` scheduler en cluster
    actor_kind: Mapped[str] = mapped_column(String(16), nullable=False)   # human|agent|scheduler|cluster|external
    origin: Mapped[str | None] = mapped_column(String(64))                # BIO2 8.15.01 "Oorsprong": IP of netwerklocatie
    summary: Mapped[str] = mapped_column(String(255), nullable=False)     # een mensleesbare regel, Nederlands
    flow_id: Mapped[str | None] = mapped_column(String(64))               # opi/core/flow_id.py
    task_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)               # async_tasks.id, GEEN vreemde sleutel
    thread_key: Mapped[str | None] = mapped_column(String(255))           # deployment:<project>/<deployment>
    dedup_key: Mapped[str] = mapped_column(String(160), nullable=False)   # signature()-vorm, zie punt 3
    data: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'"))

    __table_args__ = (
        Index("idx_notification_events_dedup", "dedup_key", "time"),
        Index("idx_notification_events_project", text("project"), text("time DESC")),
        Index("idx_notification_events_thread", text("thread_key"), text("time DESC")),
        Index("idx_notification_events_actor_sweep", "time", postgresql_where=text("actor IS NOT NULL OR origin IS NOT NULL")),
        Index("idx_notification_events_retention", "time"),
    )
```

**Toelichting bij de keuzes die niet vanzelf spreken:**

- `data` is JSONB en niet een set kolommen, want wat er in een gebeurtenis staat verschilt per soort. Dat is dezelfde afweging als `spec` in `runs` en `payload` in `async_tasks`, dus het is hier het huispatroon en geen nieuwe vondst. Wat erin mag staat in punt 4: alleen velden die de code expliciet heeft samengesteld.
- `type` komt uit een gesloten enum in code, zoals `TaskType`. Die enum is tegelijk het "centrale register voor eventtypes" dat het NL GOV-profiel vraagt, en dat hoort ergens zo opgeschreven te staan.
- `severity` gebruikt de drie waarden uit punt 3, waarvan er twee rechtstreeks uit `EventSeverity` (`event_interpreter.py:18`) komen. `noise` staat er niet bij: wat ruis is, wordt geen gebeurtenis.
- `actor_kind` en `origin` komen uit BIO2 8.15.01. `origin` is leeg bij een scheduler of het cluster, en dat is correct en geen gat: er is dan geen netwerklocatie. Bij een mens of een agent is hij verplicht te vullen, en vandaag draagt geen enkele van de betrokken logregels dat gegeven, dus dat is nieuw werk. **Waar `origin` vandaan komt is een REGEL en geen implementatiedetail, want dit veld wordt later als BEWIJS gelezen**: punt 4 geeft hem als enige kolom een eigen leesgrendel omdat hij een persoonsgegeven is, punt 6 laat hem op de zestien beveiligingssoorten jaren staan als aanvalsspoor, en deel 3, fase 4 eist hem gevuld op vijftien van die zestien. **De regel: `origin` is het adres van de PEER van de verbinding.** Een `X-Forwarded-For` telt alleen mee als die peer de bekende ingress is, en dan is de waarde de hop die die proxy zelf heeft toegevoegd, dus de meest RECHTSE die niet zelf een vertrouwde proxy is, en uitdrukkelijk niet de meest linkse. Is de peer niet de ingress, dan wordt de header genegeerd en is `origin` het peer-adres zelf. Dat tweede geval is geen theorie maar het gewone in-clusterpad: het inkomende netwerkbeleid van OPI laat op TCP/8000 `from: []` binnen (`bootstrap/rig-system/kustomize/operations-manager/overlays/odcn-production/network-policy.yaml:12-16`), dus elke pod in het cluster bereikt OPI rechtstreeks, buiten de ingress om, en daar is `origin` het pod-adres van de aanroepende werklast. **Dit is niet binnen de gebeurtenissencode alleen op te lossen, en dat hoort hier te staan voor er iemand aan begint.** Gemeten op de meetbasis `83ac4b9b`: `opi/server.py:590` zet `ProxyHeadersMiddleware(trusted_hosts=["*"])`, en bij precies die waarde zet uvicorn zelf `_TrustedHosts.always_trust` op waar. Dat laatste staat niet in deze repo maar in de afhankelijkheid die `opi/server.py:14` importeert, `uvicorn.middleware.proxy_headers`, op de meetbasis vastgezet op versie 0.40.0 (`operations-manager/python/uv.lock:2550-2551`); in die versie staat `always_trust` in `proxy_headers.py:71`, vertrouwt de middleware daarmee elke peer (`:110`), geeft `get_trusted_client_host` letterlijk `x_forwarded_for_hosts[0]` terug (`:132-133`) en overschrijft zij daarmee `scope["client"]` (`:58`). `request.client.host` is dus in elke handler al de door de aanroeper gekozen tekst, en een gebeurtenissoort die hem uitleest legt die tekst vast als oorsprong. Het versmallen van `trusted_hosts` tot de ingress hoort daarom bij dit werk, en het is een eigen wijziging met een eigen toets, want dezelfde middleware leidt ook het scheme af uit `X-Forwarded-Proto` en elke route die `request.client` leest verandert mee. Een `use-forwarded-headers`-instelling voor de ingress staat er evenmin: op de meetbasis geeft een grep op `forwarded` in `infrastructure/` en `bootstrap/` precies twee treffers, allebei van Keycloak (`infrastructure/bootstrap/infrastructure/keycloak/controller/base/deployment.yaml:55` en `:68`). **En er is niets om dit van te erven, net zoals bij ontwerpregel 4 van Kanaal 5 in deel 3.** De enige client-IP-hulpfunctie in `opi/` is `IPRateLimiter.get_client_ip` (`opi/api/router.py:140`), en zij is uitdrukkelijk NIET de functie om hier te hergebruiken, om de reden die haar eigen docstring geeft (`:146-147`): "Note: X-Forwarded-For can be spoofed. Use get_client_identifier() for robust rate limiting that combines multiple factors." Zij neemt de meest linkse waarde (`:150-155`) en valt pas daarna terug op `request.client` (`:158`), dus zij staat precies de verkeerde kant op, en zij heeft ook een andere taak: haar ene aanroeper (`opi/api/v2/router.py:623`) gebruikt haar voor snelheidsbegrenzing, waar een gespoofte waarde de aanvaller hooguit een eigen emmer oplevert, en niet voor bewijs. **Waarom dit niet tot later kan wachten.** Drie van de zestien beveiligingssoorten worden juist op een NIET-geauthenticeerd verzoek geschreven (de geweigerde allowlist-controle, de geweigerde API-sleutel en het geweigerde bearer-token; deel 3, fase 4 noemt ze bij naam), dus zonder deze regel kan een werklast in het cluster zonder enig geldig geheim een rij laten schrijven die jaren blijft staan, met de eigen locatie verborgen en het adres van een collega of een derde in het enige veld dat het incidentonderzoek moet dragen. Dat is dezelfde faalvorm die deel 3, Kanaal 2, ontwerpregel 5 een regel eerder benoemt ("dan wijst het spoor precies aan wie de aanbieder wil dat het aanwijst"), daar voor de body afgesloten en tot hier voor de header niet. Het weegt extra omdat `actor` op de sleutelwegen `NULL` is en de Actor-eis van 8.15.01 daar door `actor_kind` plus `project` plus `origin` SAMEN wordt gedragen (punt 6).
- **`actor` is leeg waar de weg geen identiteit draagt, en bij een agent hangt dat af van de weg en niet van `actor_kind`.** Een agent die een SSO-bearer-token aanbiedt DRAAGT een identiteit: `validate_user_token` legt de claims in `request.state.user` in dezelfde vorm als de sessie (`opi/api/user_token_auth.py:260`) na een geverifieerd e-mailadres te hebben geeist (`:208-213`), `get_current_user` leest precies dat (`opi/core/auth_decorators.py:57`), en `opi/core/task_helpers.py:61` zet het in `created_by`. Een agent die alleen de `X-API-Key` van zijn project aanbiedt draagt er geen: die sleutel identificeert het project en niet de handelende partij, want hij wordt uitgedeeld aan iedereen met een bewerkrol en er is er precies een per project (deel 3, ontwerpregel 5), dus de houder is niet te onderscheiden van elke andere houder ervan (deel 1 `:467`). Daar staat `actor` op `NULL`, net als bij een scheduler. Vandaag zet `opi/core/task_helpers.py:62-63` in dat geval de tekst `"API"` in `created_by`; **die tekst reist niet mee naar `actor`.** Dat is geen smaak maar dezelfde eis als in punt 6: een plaatshouder-tekst in `actor` houdt het predicaat van `idx_notification_events_actor_sweep` waar, dus krimpt die index nooit, en de pseudonimiseringspas zou een rij komen leegmaken waar nooit een persoon in stond. 8.15.01 wordt daar niet mee gebroken: die maatregel vraagt de persoon **of het proces** dat de handeling in gang zette, en dat proces staat hier in `actor_kind` plus `project`, met de netwerklocatie in `origin` ernaast. Een agent een eigen identiteit geven (een sleutel per integratie, of een token op naam) is een ontwerpvraag op zichzelf en wordt hier niet genomen; zolang de sleutel het project identificeert, is er niets door te geven. **De vijfde waarde van `actor_kind` staat hier nog open.** `external` hoort bij het inkomende endpoint uit deel 3 (de laatste fase), en wie daar in `actor` komt te staan volgt uit de authenticatie die dat endpoint kiest; deel 3 zet die keuze uitdrukkelijk voor de bouw, in Kanaal 2 ontwerpregel 5, en zij luidt daar hetzelfde als hier: `actor` volgt de WEG en niet de body, dus leeg op een sleutelweg en het geverifieerde e-mailadres op een tokenweg, met `actor_kind`, `time` en `origin` er evenmin uit de body. Zolang die route niet bestaat, bestaat er geen rij met `actor_kind = external`.
- `task_id` is met opzet geen vreemde sleutel: de rij in `async_tasks` verdwijnt na een uur en een `ON DELETE`-regel zou de gebeurtenis mee de afgrond in trekken.
- **Geen `occurrences` en geen `last_seen_at` op deze tabel**, en geen uniciteitsgrendel op `dedup_key`. Beide zouden een `UPDATE` op een alleen-invoegen tabel betekenen; ze staan op de ontvangerrij hieronder. Zie de beslechting in punt 3. De index `idx_notification_events_dedup` blijft wel, want de meldkant groepeert daarop.
- `idx_notification_events_actor_sweep` is de index waarop de pseudonimiseringspas van de lus uit punt 6 draait, dus de pas die na 90 dagen leegmaakt en niet de pas die na een jaar opruimt, en hij krimpt vanzelf: zodra een rij is gepseudonimiseerd valt hij eruit, want de lus zet allebei de kolommen op `NULL` en niet op een plaatshouder-tekst. Punt 6 legt uit waarom dat onderscheid hier dragend is: met een tekst in `actor` blijft dit predicaat waar en krimpt de index nooit. Hij staat op allebei de persoonsgegevens, want de lus wist `actor` en `origin` samen. De uitzondering voor de beveiligingssoorten zit niet in de index maar in de `WHERE` van de lus, als een `type <> ALL (...)` op de gesloten lijst uit punt 6, en die geldt voor allebei de passes: de lus leest dus `type` en niet `category`, en er is geen extra kolom voor nodig.
- `idx_notification_events_retention` is de index voor de tweede pas, die na een jaar opruimt. Die pas kan de sweep-index niet gebruiken, want zijn rijen dragen dan geen `actor` en geen `origin` meer en vallen dus buiten dat predicaat, en de rijen van een scheduler of het cluster stonden er sowieso nooit in: `actor` draagt een e-mailadres en die twee hebben er geen, en `origin` is er bij allebei leeg omdat er geen netwerklocatie is. Dat het een scheduler was staat in `actor_kind`, buiten dit predicaat. Een agent op de projectsleutel hoort NIET bij die groep: zijn `actor` is om de reden hierboven ook leeg, maar zijn `origin` niet, dus hij staat wel in de sweep-index totdat de eerste pas hem leegmaakt. Punt 6 legt dat uit. Een gewone index op `time` is genoeg, en het is dezelfde zet als `idx_async_tasks_completed` bij `cleanup_old_tasks`, het patroon waarvan deze lus de vorm leent.

### `notification_deliveries` (VOORSTEL): de rij per persoon

```python
class NotificationDelivery(Base):
    """Wat er voor EEN persoon is neergelegd, met leesstatus en reden."""

    __tablename__ = "notification_deliveries"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, server_default=text("gen_random_uuid()"))
    event_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("notification_events.id", ondelete="CASCADE"), nullable=False
    )
    recipient: Mapped[str] = mapped_column(String(255), nullable=False)   # e-mailadres, kleine letters
    reason: Mapped[str] = mapped_column(String(64), nullable=False)       # project-admin|project-member|actor|approver|platform-admin|platform-announcement
    dedup_key: Mapped[str] = mapped_column(String(160), nullable=False)   # gedenormaliseerde kopie van notification_events.dedup_key
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    occurrences: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("idx_notification_deliveries_unique", "event_id", "recipient", unique=True),
        Index(
            "idx_notification_deliveries_unread",
            "recipient",
            postgresql_where=text("read_at IS NULL AND archived_at IS NULL"),
        ),
        Index("idx_notification_deliveries_inbox", text("recipient"), text("created_at DESC")),
        Index("idx_notification_deliveries_dedup", "recipient", "dedup_key", postgresql_where=text("read_at IS NULL")),
    )
```

**De partiele index `idx_notification_deliveries_unread` is de belangrijkste regel van dit hele datamodel.** Dat is de index waarop de teller in de kop van elke pagina draait. Zonder hem is die teller een tabelscan bij elke paginaweergave; met hem is het een indexlezing over een index die alleen de ongelezen rijen bevat, en die is klein omdat mensen hun postvak leegmaken. Dezelfde vorm als `idx_runs_active` in `opi/services/persistence/runs.py`, dus het patroon ligt er al.

`reason` is de kolom die richting B niet kan hebben: hij bewaart WAAROM deze persoon deze melding kreeg, op het moment dat dat gold. Vandaar de vaste waardenlijst en niet een vrije tekst. De lijst telt zes waarden, en de zesde is van een andere orde dan de vijf andere: die vijf noemen allemaal een ROL of een betrokkenheid die JIJ had (beheerder of lid van dit project, de actor, de beoordelaar, de platformbeheerder), en `platform-announcement` noemt er geen enkele, want je krijgt hem omdat je gebruiker van dit platform bent en om niets anders. Dat is de ene uitzondering uit punt 4, en zij staat hier omdat de tekst "waarom kreeg ik dit bericht" (deel 3 `:384`) uit deze kolom komt: zonder een eigen waarde zou een platformmededeling `platform-admin` moeten dragen, en dan zegt het postvak van een projectlid dat hij platformbeheerder is.

`occurrences` en `last_seen_at` staan hier en niet op de gebeurtenis, en `idx_notification_deliveries_dedup` is de index die de vraag beantwoordt waar ze voor dienen: heeft deze ontvanger al een ONGELEZEN melding met deze dedupsleutel. Zo ja, dan hoogt de meldkant de teller op en zet `last_seen_at` bij; zo nee, dan komt er een nieuwe rij. Daarvoor staat `dedup_key` ook in het blok hierboven, als gedenormaliseerde kopie van de kolom op de gebeurtenis: zonder die kopie is elke tellervraag een join naar de gebeurtenissentabel. Dat is de prijs en hij is klein, want de sleutel wordt bij het schrijven van de gebeurtenis berekend en daarna nooit meer aangeraakt (punt 3), dus de kopie kan niet uit de pas gaan lopen.

### `notification_channel_deliveries` (VOORSTEL): de outbox

Apart van de ontvangerrij, want een persoon kan een melding via twee kanalen krijgen en die kunnen los van elkaar slagen of falen.

```python
class NotificationChannelDelivery(Base):
    """Een aflevering van EEN melding over EEN kanaal. Dit is de outbox."""

    __tablename__ = "notification_channel_deliveries"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, server_default=text("gen_random_uuid()"))
    delivery_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("notification_deliveries.id", ondelete="CASCADE"), nullable=False
    )
    channel: Mapped[str] = mapped_column(String(32), nullable=False)      # email|mattermost
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'pending'"))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    claimed_by: Mapped[str | None] = mapped_column(String(255))           # instance_id, zoals async_tasks
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index("idx_notification_channel_unique", "delivery_id", "channel", unique=True),
        Index(
            "idx_notification_channel_due",
            "next_attempt_at",
            postgresql_where=text("status = 'pending'"),
        ),
    )
```

De uniciteitsgrendel op `(delivery_id, channel)` is de idempotentie bij het aanleggen: twee keer dezelfde aflevering plannen levert een rij op. `claimed_by` en `claimed_at` zijn de claim die meerdere OPI-processen uit elkaar houdt, in dezelfde vorm die `async_tasks` al gebruikt (`instance_id` in `AsyncTaskService.__init__`). De partiele index op `next_attempt_at` is wat de planner elke tik bevraagt, en hij is klein want alleen wachtende rijen staan erin.

**De webhook staat hier bewust niet bij, en dat is geen omissie.** Deze tabel hangt via `delivery_id` aan een rij die voor EEN PERSOON is neergelegd, en de webhook per project heeft geen persoon: hij hoort bij een projectabonnement. De webhook loopt daarom niet over deze outbox maar over het watermerk uit punt 3 (`reported_through` per abonnement, alles sinds het watermerk gegroepeerd). Dat is dezelfde reden dat hij ook niet in de persoonlijke voorkeuren staat. Zie deel 3, Kanaal 5.

`sent_at` betekent hier "het kanaal heeft hem aangenomen" en niet "afgeleverd". Voor mail is dat verschil echt en gemeten (deel 3, Kanaal 3, punt 3 over bounces), en het hoort in de UI ook zo te staan.

### `notification_preferences` (VOORSTEL): de voorkeuren

```python
class NotificationPreference(Base):
    """Wat EEN persoon per type per kanaal wil. Afwezig = het standaardprofiel van zijn rol."""

    __tablename__ = "notification_preferences"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, server_default=text("gen_random_uuid()"))
    recipient: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)     # een van de twaalf typen
    channel: Mapped[str] = mapped_column(String(32), nullable=False)      # inbox|email|mattermost
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )

    __table_args__ = (
        Index("idx_notification_preferences_unique", "recipient", "category", "channel", unique=True),
    )
```

**Afwezigheid betekent de standaard van de rol, en niet "uit".** Dat is een echte keuze: het alternatief is bij het aanmaken van een gebruiker 12 typen keer 3 kanalen aan rijen schrijven, en dan is een wijziging in de standaarden niet meer door te voeren bij bestaande gebruikers. Zo staat er alleen in de tabel wat iemand bewust anders heeft gezet.

### Twee tabellen die hier bewust niet staan

Ze horen bij een kanaal en niet bij de kern, en ze komen daarom in deel 3 aan bod:

- **`notification_subscriptions`** (VOORSTEL) voor de webhook per project: ontvanger-URL, geheim, welke categorieen, en het watermerk `reported_through` uit punt 3. De projectkolom van dat abonnement is een grendel en niet alleen een groepering: een abonnement levert uitsluitend gebeurtenissen van zijn eigen project, en de categorieenkeuze filtert binnen die scope in plaats van hem op te rekken (deel 3, Kanaal 5, ontwerpregel 3). Dit is een abonnement van een PROJECT en niet van een persoon, en het draait daarom op het watermerk en niet op de outbox hierboven. De ontvanger-URL is de enige waarde in dit hele model die bepaalt waar OPI zelf een verbinding heen opzet, en zij komt van een gebruiker; wat er in die kolom mag staan en wie er een rij mag neerzetten zijn daarom ontwerpregels en geen validatiedetails. Ze staan in deel 3, Kanaal 5, als ontwerpregel 4 (welke bestemmingen zijn toegestaan, en waarom het uitgaande netwerkbeleid daar niets aan bijdraagt) en ontwerpregel 5 (aanmaken vraagt een projectbeheerder met identiteit en gaat uitdrukkelijk niet op de `X-API-Key`).
- **`notification_channel_identities`** (VOORSTEL) voor Mattermost: `recipient`, `channel`, `external_id` en `verified_at`, gevuld via de zelfkoppeling met verificatiecode.

### De migratieweg

De modules komen onder `opi/services/persistence/` en worden geimporteerd in `opi/services/persistence/__init__.py`, want dat is wat ze op `Base.metadata` zet en dus zichtbaar maakt voor Alembic (`include_orm_object` in `opi/core/db.py:112` beperkt autogenerate tot precies die tabellen).

De migratie wordt de vijfde in de rij, `opi/migrations/versions/005_add_notifications.py` (VOORSTEL), met `down_revision = "004"`.

**Let op een verschil met wat het startpunt aanneemt.** Het startpunt zegt "autogenerate is gericht op de ORM-modellen", en dat klopt. Maar de vier bestaande migraties doen het niet zo: ze voeren een `op.execute()` uit op een SQL-constante uit `opi/core/*_schema.py` (zie `004_add_runs.py`, dat `RUNS_TABLE_SQL` uitvoert). Dat is een overblijfsel van de tijd voor de ORM en het is bewust gedocumenteerd in `opi/core/db.py:1`.

Voor een nieuwe tabel is er geen SQL-constante om te erven, dus hier is de keuze vrij, en dat is een beslissing die iemand moet nemen:

- **Autogenerate en de ORM als bron.** Schoner, en het is de richting die `db.py` zelf beschrijft ("makes it the schema-as-code source of truth"). Nadeel: het wijkt af van de vier migraties die er liggen.
- **Een `NOTIFICATIONS_TABLE_SQL` en `op.execute()`.** Consistent met wat er ligt. Nadeel: het is twee keer dezelfde waarheid schrijven, en dat is precies waar `db.py` vanaf wilde.

**Aanbeveling: autogenerate.** Deze tabellen hebben geen historie om te dragen, de schema-driftcontrole (`operations-manager/python/scripts/check_orm_schema.py`) bewaakt het al, en het is de kant waar het codebestand naartoe beweegt. Wel de gegenereerde migratie nalezen: autogenerate zet partiele indexen niet altijd goed neer, en die zijn hier niet decoratief.

---

## Wat er nog aan open randen ligt

- Of er een organisatie-OIN is voor RIG. Zonder die is een geldige CloudEvents `source` niet te bouwen en blijft regel 1 uit de beslechting hierboven van kracht. **Niet geverifieerd**, en niet in deze repo te vinden.
- Hoe lang "langer" is voor de beveiligingsgebeurtenissen. Dat is niet vanuit de code te beslissen; het is een gesprek met wie verantwoordelijk is voor de risicoafweging in `features/bio-network-access-no-vpn-compliance.md`.
- Of de tijdlijn per deployment de `deviations` en `errors` uit `opi/services/deployment_diagnostics.py` moet gaan vastleggen als overgangen, of dat die berekening blijft wat hij is en er alleen een gebeurtenis wordt geschreven bij een verandering. Dat laatste is goedkoper maar vraagt een vergelijking met de vorige stand, en die stand is er vandaag niet.
- Of de wizard, die nog via de in-memory `task_manager` loopt, gebeurtenissen kan schrijven voordat `features/futures/migrate-task-progress-to-database.md` is afgerond. De `PersistentTaskProgressManager` bestaat inmiddels (`opi/core/persistent_task_progress.py`) en wordt door de TaskWorker gebruikt, dus dat toekomstdocument beschrijft werk dat gedeeltelijk gedaan is; hoeveel precies is **niet geverifieerd**.
- Waar `origin` (BIO2 8.15.01) vandaan komt op de wegen die hem vandaag niet dragen. **Dit stond hier als open rand met de aantekening "niet geverifieerd"; het is meetbaar, het is inmiddels gemeten, en de uitkomst staat als regel bij de `origin`-kolom in punt 7.** Kort: `ProxyHeadersMiddleware` staat op `trusted_hosts=["*"]` (`opi/server.py:590`), dus `request.client.host` is vandaag de meest linkse `X-Forwarded-For` en daarmee door de aanroeper te kiezen; punt 7 zegt wat `origin` in plaats daarvan is en wat er buiten de gebeurtenissencode voor moet gebeuren. Wat hier open BLIJFT is niet de meting maar de uitvoering: het versmallen van `trusted_hosts` tot de ingress raakt elke route die `request.client` leest en ook de scheme-afleiding uit `X-Forwarded-Proto`, dus dat is een eigen wijziging met een eigen toets en niet iets dat in fase 4 ongemerkt langszij komt.
