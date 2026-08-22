# Meldingen in ZAD: de oplossingsrichtingen naast elkaar

**Geschreven op**: 22 augustus 2026. Dit is deel 2 van drie; de eventcatalogus staat in
`plans/meldingen-inventarisatie.md`, de kanalen en het plan in
`plans/meldingen-plan-van-aanpak.md`.

Vier richtingen, alle vier op dezelfde assen. Daarna de aanbeveling, en daarna de vijf vragen
die binnen de gekozen richting hoe dan ook beantwoord moeten worden: waar de gebeurtenis
ontstaat, hoe hij betrouwbaar aankomt, hoe je ontdubbelt, hoe hij zich tot het audittrail
verhoudt, en hoe het datamodel eruitziet.

**Een naamconventie vooraf.** Alles wat hieronder een naam draagt die vandaag niet in de code
staat, is een **voorstel** en wordt als zodanig gemarkeerd. Dat geldt voor tabelnamen,
kolomnamen, endpoints en eventnamen. Er staat niets vast tot iemand er ja tegen zegt.

## De assen

| As | Wat de vraag is |
|---|---|
| **Bouwen** | wat het kost om het de eerste keer neer te zetten |
| **Draaien** | wat het per dag kost aan opslag, schrijfwerk en onderhoud |
| **Schalen** | wat er gebeurt bij tien keer zo veel projecten of gebeurtenissen |
| **Gebruiker** | wat de persoon er werkelijk aan heeft |
| **Falen** | wat er stukgaat en hoe je dat merkt |
| **Later** | wat het blokkeert en wat het openhoudt |

---

## Richting A: doorgeefluik zonder postvak

**Wat het is.** Een gebeurtenis gaat rechtstreeks naar een kanaal (mail, Mattermost, ntfy) en
verder nergens heen. Geen tabel, geen leesstatus, geen geschiedenis. De code roept bij een
mislukte deploy een functie aan die een bericht opstelt en verstuurt, en daarmee is de
gebeurtenis voorbij.

**Bouwen.** Het goedkoopst van de vier, met afstand. Eén verzendmodule, één plek die uitrekent
wie het bericht moet krijgen, en per gebeurtenis een aanroep. Er is geen migratie, geen model,
geen UI. Realistisch: één PR-serie voor de eerste vijf gebeurtenissen.

**Draaien.** Bijna niets. Geen opslag, geen opruiming, geen bewaartermijn.

**Schalen.** Slecht op een manier die pas laat pijn doet. Zonder opslag is er geen
ontdubbeling over herstarts heen (hetzelfde probleem als de ingebouwde logbewaker, zie deel 1
paragraaf 9), en zonder wachtrij is elke uitbarsting een uitbarsting in de mailbox van de
gebruiker. Twintig herstarts is dan echt twintig mails.

**Gebruiker.** Hier valt de richting. Eerlijk opgesomd wat je niet hebt:

- **geen leesstatus**, dus geen "wat is er nieuw sinds gisteren";
- **geen geschiedenis**, dus de vraag "wat is er vannacht met mijn project gebeurd" is alleen
  te beantwoorden door je mailbox te doorzoeken;
- **geen bewijs**, dus "ik heb daar nooit iets over gehoord" is niet te weerleggen;
- **geen UI**, dus de eis "terug te zien in de UI en de API" uit de wens vervalt;
- **geen zinnige voorkeuren.** Dit is het subtielste verlies. Een instelling "geen mail voor
  uitrol" betekent zonder postvak dat die gebeurtenis voor die persoon volledig verdwijnt. De
  keuze is dan niet "waar wil ik het horen" maar "wil ik het weten, ja of nee", en dat is een
  andere en veel slechtere vraag.

**Falen.** Een kanaal dat plat ligt betekent verlies. De mailrelay die een uur weg is, kost
een uur meldingen, definitief. Er is niets om opnieuw te proberen, want er is niets bewaard.
En het faalt stil: niets in het systeem weet dat er iets weg is.

**Later.** Het blokkeert alles wat later gewenst is. Een postvak toevoegen is geen uitbreiding
maar een herbouw: de emitpunten blijven staan, maar alles wat erachter zit (route, model,
aflevering, dedup, voorkeuren) wordt opnieuw gedaan. Het houdt precies één ding open: het is
snel weer weg te halen.

**Waar dit wel het goede antwoord is.** Voor de logbewaker. Die stuurt vandaag naar ntfy zonder
opslag, en dat is de juiste keuze voor dat geval: het publiek is één beheerder, het bericht is
vluchtig, en de geschiedenis staat toch al in Loki. Richting A is dus niet fout, hij is al in
gebruik voor het enige geval waar hij past.

---

## Richting B: eventlogboek met uitwaaieren bij lezen

**Wat het is.** Eén onveranderlijke tabel met gebeurtenissen. Wie wat ziet, volgt uit een
bevraging op het moment van kijken: neem de gebeurtenissen, filter op de projecten waar deze
persoon lid van is, filter op de typen die hij aan heeft staan, en toon wat overblijft.
Niemand krijgt een eigen rij.

**Bouwen.** Middelmatig. Eén tabel, één bevraging, één UI. De autorisatie zit in de bevraging
en dat is werk, maar het is werk dat er in de projectlaag al ligt
(`opi/services/project_authorization.py`). Geen uitwaaierlogica bij het schrijven, dus de
schrijfkant is de eenvoudigste van de drie echte richtingen.

**Draaien.** Zuinig. Eén rij per gebeurtenis, hoe veel belanghebbenden er ook zijn. Voor een
gebeurtenis met acht projectleden scheelt dat een factor acht ten opzichte van richting C.

**Schalen.** Hier zit de kwestie, en hij is tweeledig.

De bevraging zelf is te doen: een index op tijd plus een filter op een lijst projectnamen is
een gewone query. Maar de **ongelezen-teller** is het probleem. Die staat in de kop van elke
pagina, dus hij wordt bij elke paginaweergave gesteld, en hij is niet te beantwoorden met een
gewone `count`: je moet de hele bevraging uitvoeren en er vervolgens de leesstatus overheen
leggen. Bij één gebruiker is dat niets, bij honderd gebruikers die elk elke minuut een pagina
laden is het de duurste query van het portaal.

En de leesstatus zelf, want die is per persoon. Er zijn drie manieren, en alle drie hebben
een prijs:

1. **Eén "gelezen tot"-tijdstip per persoon.** Bijna gratis, maar het is een streep en geen
   status: je kunt niet één melding wegklikken en de rest laten staan. Dat is minder dan wat
   de wens beschrijft.
2. **Een koppeltabel `melding_gelezen` (voorstel) van persoon naar gebeurtenis.** Dan is er
   toch een rij per persoon per gebeurtenis, maar alleen voor wat gelezen is. Zuiniger dan
   richting C, en het besparingsargument van B is grotendeels weg.
3. **Een vinkje per persoon in een JSONB-veld op de gebeurtenis.** Werkt tot het aantal
   lezers groeit, en dan is elke leeshandeling een schrijfoperatie op een rij die anderen ook
   lezen.

**Gebruiker.** Bijna even goed als richting C, met één gat: **"waarom zie ik dit" is niet te
beantwoorden.** De reden is de bevraging, en die is niet vastgelegd. Je kunt hem opnieuw
uitrekenen ("omdat je beheerder bent van project X"), maar dan geef je de reden van NU en niet
die van toen. En bij een gebeurtenis met meerdere redenen (je bent lid EN je startte de taak
zelf) is er geen manier om te zeggen welke ertoe deed.

**Falen.** Het faalt op een prettige manier: als er iets misgaat in de bevraging, mist iemand
een melding maar is de gebeurtenis niet weg. De tabel is onveranderlijk, dus een fout in de
autorisatieregel is repareerbaar zonder gegevensverlies. Dat is een echt voordeel en het is
er precies één.

**Het echte bezwaar tegen B, en dat is geen prestatiebezwaar.** De bevraging kijkt naar het
HEDEN. Iemand die vandaag lid wordt van een project ziet daarmee alle meldingen van de
afgelopen maanden over dat project, inclusief mislukte deploys van voor zijn tijd. En iemand
die eruit gaat, verliest per direct alle meldingen die hij ooit heeft gekregen, ook die over
zijn eigen handelingen. Beide zijn fout, en beide zijn niet te repareren zonder de
lidmaatschapsgeschiedenis mee te nemen in de bevraging. Die geschiedenis bestaat vandaag niet
in een bevraagbare vorm (lidmaatschap is een YAML-lijst; de geschiedenis is de git-log van het
projectbestand). Dat is een tweede systeem bouwen om het eerste te laten kloppen.

Ter herinnering uit deel 1: `cross-domain-access` maakt het nog scherper. Daar is de
autorisatieregel niet "lid van het project van de gebeurtenis" maar "lid van een van de twee
projecten die de gebeurtenis noemt". Elke uitzondering op de regel moet in de bevraging, en de
bevraging is precies de plek waar je geen uitzonderingen wilt.

**Later.** Houdt veel open. Een onveranderlijke eventtabel is een goede basis voor van alles,
en er kan later een postvak bovenop (dan wordt B de onderlaag van C). Blokkeert niets.

---

## Richting C: postvak per persoon, het GitHub-model

**Wat het is.** Bij het ontstaan van een gebeurtenis wordt uitgerekend wie hem moet zien, en
per belanghebbende wordt een rij geschreven, met leesstatus, de reden ("waarom zie ik dit") en
een draadsleutel per onderwerp. Wat je ziet is wat er voor jou is neergelegd, op het moment
dat het gebeurde.

**Bouwen.** Het duurst. Twee tabellen, de uitwaaierlogica, de reden per ontvanger, de draden,
de voorkeurentabel, en het scherm. Realistisch: drie tot vier PR-series voor iets wat af is,
al past de eerste fase (zie deel 3) in één.

**Draaien.** Duurder in opslag en schrijfwerk. Een gebeurtenis met acht belanghebbenden is
acht rijen. Om te weten of dat erg is, moet je het rekenen in plaats van te vrezen. Grof
geschat op de huidige omvang van het platform: enkele tientallen projecten, gemiddeld een
handvol leden, en een orde van tientallen meldingswaardige gebeurtenissen per project per dag
op een drukke dag. Dat komt uit op enkele duizenden rijen per dag. Een rij is orde honderden
bytes. Dat is enkele honderden megabytes per jaar bij ONGELIMITEERDE bewaring, en met een
bewaartermijn van negentig dagen is het een tabel van tientallen megabytes. **Dat is geen
schaalprobleem, dat is een tabel.** Het schrijfwerk idem: acht `INSERT`s in één transactie is
één rondgang.

**Schalen.** Het schaalt van de vier het best, en om een reden die contra-intuïtief is: het
duurste werk (uitrekenen wie iets moet zien) gebeurt één keer, bij het ontstaan, terwijl het
vaakste werk (het postvak tonen, de teller ophalen) een indexlezing op één kolom is. De
ongelezen-teller is `SELECT count(*) WHERE ontvanger = ? AND gelezen_op IS NULL`, met een
partiële index precies daarop. Dat is de goedkoopste vorm die er is, en juist die query is de
vaakste.

**Gebruiker.** Dit is wat de wens beschrijft. Leesstatus per persoon, geschiedenis die niet
verandert als je lidmaatschap verandert, een reden bij elke melding, en draden zodat twintig
gebeurtenissen over dezelfde deployment één regel in het postvak zijn.

**Falen.** Twee echte manieren.

- **De uitwaaiering is fout op het moment van schrijven, en dat is dan definitief.** Wie
  vergeten is, blijft vergeten: er is geen tweede kans zoals bij B, waar je de bevraging
  repareert en iedereen alsnog ziet wat hij moest zien. Tegenmaatregel: de gebeurtenis en de
  ontvangers zijn twee tabellen (zie het datamodel), dus de gebeurtenis blijft staan en de
  uitwaaiering is over te doen.
- **De uitwaaiering staat op het kritieke pad.** Als het uitrekenen van de ontvangers faalt of
  traag is, raakt dat de handeling zelf. Tegenmaatregel: de outbox (zie hieronder). De
  gebeurtenis wordt in dezelfde transactie weggeschreven als de handeling; het uitwaaieren
  gebeurt erna, door de werker.

**En de vraag die de opdracht terecht stelt: wat met iemand die na het feit lid wordt, of geen
lid meer is?** Het antwoord van richting C is helder en het is de reden dat C wint:

- **Wie na het feit lid wordt, krijgt de oude meldingen niet.** Ze zijn nooit voor hem
  neergelegd. Dat is correct: hij was er niet, hij hoefde het niet te weten, en hij hoeft de
  mislukte deploys van vorige maand niet als ongelezen in zijn postvak te vinden. Wil hij
  weten wat er is gebeurd, dan is dat de geschiedenis van het project en niet zijn postvak.
- **Wie geen lid meer is, houdt zijn oude meldingen.** Ze waren voor hem, hij heeft ze
  gekregen, en ze gaan over dingen die hij zelf deed of moest weten. Nieuwe krijgt hij niet
  meer, want de uitwaaiering vraagt bij elke nieuwe gebeurtenis opnieuw wie lid is.
- **De ene uitzondering die expliciet moet.** Bij "je bent uit dit project verwijderd" is de
  ontvanger op het moment van uitwaaieren juist GEEN lid meer. Die melding moet dus buiten de
  gewone lidmaatschapsregel om worden neergelegd, en dat kan alleen in een model dat per
  ontvanger schrijft. In richting B is deze melding niet te bezorgen, punt.

**Later.** Houdt het meeste open, blokkeert één ding: het is de duurste om terug te draaien.
Maar de gebeurtenistabel eronder is dezelfde als in richting B, dus terugvallen op B is een
bevraging vervangen en de ontvangertabel laten staan.

---

## Richting D: het CloudEvents-jasje

**Wat het is.** Geen alternatief voor B of C maar een keuze bínnen de gekozen richting: leg
het eventrecord vast in het NL GOV-profiel voor CloudEvents (Logius, v1.1 vastgesteld; op de
lijst van het Forum Standaardisatie staat CloudEvents v1.0 als pas-toe-of-leg-uit). Concreet
betekent dat: de kolommen van de gebeurtenistabel dragen de namen en de vorm van het profiel.

Het profiel eist vier attributen:

| Attribuut | Eis van het profiel | Wat dat bij ons zou zijn |
|---|---|---|
| `id` | uniek, bij voorkeur domeinspecifiek, anders UUIDv4 | de primaire sleutel, `gen_random_uuid()` zoals elke andere tabel hier |
| `source` | URN met `nld`-namespace: `urn:nld:oin:<OIN>:systeem:<naam>` | vraagt een OIN. **Die hebben wij niet vastgesteld** en het is geen technische keuze |
| `specversion` | `"1.0"` | een constante |
| `type` | omgekeerde domeinnotatie, met `v`-prefix voor versies | `nl.zad.deployment.mislukt.v1` in plaats van `deployment-mislukt` |

Optioneel maar relevant: `subject` (waar het over gaat, zodat een afnemer kan filteren zonder
de payload te openen), `time` (RFC 3339), `dataschema`, en `dataref` voor het claim
check-patroon.

**Bouwen.** Vrijwel gratis ALS je het meteen doet. Het is een keuze in kolomnamen en in een
naamgevingsregel voor eventtypen. Er is een `cloudevents`-SDK voor Python, maar die is niet
nodig om de vorm aan te houden; hij is nodig zodra je events over HTTP uitwisselt.

**Bouwen, later.** Duur. De eventtypen staan dan in de code, in de voorkeuren van iedere
gebruiker, en in de opgeslagen rijen. Omzetten is een migratie plus een vertaaltabel plus een
periode waarin beide vormen bestaan.

**Draaien.** Geen verschil. Een kolom heet anders.

**Gebruiker.** Nul. De gebruiker ziet dit nooit.

**Falen.** Eén reëel risico: het profiel is streng over wat er in de context-attributen mag
staan. "Geen gevoelige data in context-attributen, want die zijn inspecteerbaar en worden
gelogd door tussenliggende systemen." Bij ons zou `subject` de projectnaam of de
deploymentnaam zijn, en dat is geen persoonsgegeven. Maar het is wel de regel die je overtreedt
zodra iemand het e-mailadres van de actor in `subject` zet omdat het handig staat. De regel
overnemen is dus zelf een voordeel.

**Later.** Dit is het hele punt. Zonder het profiel is een koppelvlak naar buiten (een
webhook, een abonnement voor een andere overheidspartij, een gemeenschappelijke
notificatiedienst) een verbouwing. Met het profiel is het een endpoint.

**Wat NIET overnemen.** De Abonneren-standaard en de Notificatieservices-API. Die gaan over
het aanbieden van abonnementen aan derden, en dat willen we niet en hebben we niet nodig. De
werkversie van Abonneren is bovendien nog niet vastgesteld, en de Notificatieservices-repo
heeft helemaal geen publicatie. Overnemen wat nog beweegt is het slechtste van twee werelden.

**Wat er open blijft staan.** De `source` vraagt een OIN (Organisatie-Identificatienummer). Dat
is een vraag aan de organisatie en niet aan de bouwer. Zolang die er niet is, kan `source` een
plaatshouder zijn met een vaste vorm die later één keer wordt ingevuld. Dat is precies het
soort ding dat je nu goedkoop regelt en later niet meer.

---

## De vergelijking in één tabel

| As | A: doorgeefluik | B: logboek + bevraging | C: postvak per persoon | D: CloudEvents-vorm |
|---|---|---|---|---|
| Bouwen | zeer laag | middel | hoog | verwaarloosbaar (nu), hoog (later) |
| Draaien | verwaarloosbaar | laag | laag (tientallen MB) | geen verschil |
| Schalen | slecht (uitbarstingen) | matig (de teller is de duurste query) | goed (teller is een indexlezing) | geen verschil |
| Gebruiker | onvoldoende voor de wens | goed, maar "waarom zie ik dit" ontbreekt | wat de wens beschrijft | onzichtbaar |
| Falen | verlies bij een kapot kanaal | vergevingsgezind, herstelbaar | uitwaaiering is eenmalig; outbox dekt het | strengere regels rond persoonsgegevens (gunstig) |
| Later | blokkeert alles | blokkeert niets | duurste om terug te draaien | opent het koppelvlak |

## De aanbeveling

**Richting C, in de vorm van D, met de gebeurtenistabel van B eronder.**

Dat is niet een compromis maar de constatering dat B en C dezelfde onderlaag hebben. Twee
tabellen: een onveranderlijke gebeurtenistabel (dat is B) en een ontvangertabel die per persoon
een rij draagt met leesstatus en reden (dat is wat C toevoegt). Wie later B alleen wil, laat de
tweede tabel leeg. De kolomnamen van de eerste tabel volgen het NL GOV-profiel (dat is D).

**Waarom A afvalt.** Omdat de wens letterlijk om leesstatus, om een UI en om per-persoon
instellingen vraagt, en A geeft geen van drieën. En omdat een melding die verdwijnt als de
mailrelay even weg is, geen melding is. A blijft wel bestaan waar hij past: de logbewaker naar
ntfy verandert niet.

**Waarom B alleen afvalt.** Om drie dingen, in volgorde van zwaarte:

1. **De melding "je bent uit dit project verwijderd" is niet te bezorgen.** De ontvanger is
   op het moment van kijken geen lid meer, dus de bevraging sluit hem uit. Dat is geen randgeval
   dat je later oplost; het is een gebeurtenis uit de inventarisatie die in richting B principieel
   onbezorgbaar is.
2. **De geschiedenis beweegt mee met het lidmaatschap.** Wie vandaag lid wordt, erft
   maandenlang ongelezen meldingen; wie vertrekt, verliest zijn eigen geschiedenis. Repareren
   vraagt een bevraagbare lidmaatschapsgeschiedenis, en die bestaat niet.
3. **"Waarom zie ik dit" is niet te beantwoorden**, want de reden is de bevraging en die is
   niet vastgelegd.

De eerste twee zijn correctheidsbezwaren en geen smaakbezwaren. Daarom valt B af als eindbeeld,
en niet als onderlaag.

**Waarom D geen aparte richting is.** Omdat hij niets kost als je hem meeneemt en veel kost
als je hem overslaat. Het enige wat hij nu vraagt is een besluit over de `source`-URN en een
naamgevingsregel voor eventtypen. Neem het niet blind over: de Abonneren-standaard en de
notificatiedienst-API blijven buiten de deur, en het profiel wordt gevolgd op het RECORD, niet
op de architectuur.

---

Wat hieronder volgt geldt binnen de aanbevolen richting.

## 1. Waar de gebeurtenis ontstaat

**Dit is de belangrijkste architectuurkeuze in het hele stuk.** Er zijn drie manieren en de
verleiding is om de mooiste te kiezen.

### De drie manieren

**a. Losse `emit()`-aanroepen door de code heen.** Op de plek waar iets gebeurt staat een regel
die een gebeurtenis vastlegt. `await emit(EventType.DEPLOYMENT_MISLUKT, project=..., ...)`.

**b. Declaratief via het bestaande hakensysteem.** Een dienst declareert zijn eigen events
zoals hij nu al zijn eigen goedkeuringen declareert: `@on(ActionEvent.X)` in
`opi/services/catalog/events.py`.

**c. De opslagweg vergelijkt.** Bij het opslaan van een projectbestand wordt oud tegen nieuw
gelegd en daar rollen gebeurtenissen uit.

### Waarom b vandaag niet kan, en dat is een meting en geen mening

Het hakensysteem is echt en het is goed gebouwd: één decorator, een index in de registry, één
payload-object per event, en `features/service-event-hooks.md` beschrijft het contract. Maar
`ActionEvent` heeft **twee** leden: `AFTER_SYNC` en `REDEPLOY`
(`opi/services/services_enums.py:176`). En `UIEvent` heeft er drie, alle drie over weergave.

Dat is geen gebeurtenisbus. Het is een uitbreidingspunt in de uitrolcyclus: `AFTER_SYNC` vuurt
één keer per deployment na de synchronisatie, `REDEPLOY` per component waar nieuwe inhoud op
is gezet (`_EVENT_LEVELS`, `services_enums.py:220`). Bijna geen enkele gebeurtenis uit de
inventarisatie past daarop. Een mislukte backup, een goedgekeurde aanvraag, een verwijderd
projectlid, een verlopen console: geen ervan gebeurt in de uitrolcyclus.

Er is bovendien een contract dat botst. Een `ActionEvent`-handler **commit nooit**; de
aanroeper doet één commit voor de hele ronde. Dat is precies goed voor "muteer het
projectbestand" en precies verkeerd voor "leg een gebeurtenis vast", want een gebeurtenis moet
juist meecommitten met de handeling die hem veroorzaakte (zie de outbox hieronder).

**Conclusie: het hakensysteem uitbreiden met een derde familie is een echte optie, maar het is
een verbouwing van dat systeem en geen gebruik ervan.** En de verbouwing levert alleen iets op
voor de gebeurtenissen die uit een DIENST komen. Dat zijn er in de inventarisatie een handvol.
De rest komt uit de takenwerker, de planners, de goedkeuringsmachine en de opslagweg, en die
zijn geen dienst.

### Wat wel

**Aanbeveling: a als basis, c voor het projectbestand, en b alleen als een dienst iets weet dat
niemand anders weet.**

- **a. Een losse aanroep, op de plek waar het gebeurt.** Niet mooi, wel eerlijk. Het is de
  enige vorm die op alle bronnen uit de inventarisatie werkt, en hij is direct te lezen: op de
  regel waar de taak faalt, staat dat er een melding uit komt. De prijs is dat een vergeten
  aanroep een stille lacune is. Die prijs is beheersbaar door de aanroepen te bundelen op de
  weinige plekken waar de levenscyclus samenkomt: de takenwerker (één plek voor 23 taaksoorten
  keer drie eindtoestanden), de goedkeuringsmachine (`apply_approval_verdicts`, één plek voor
  alle oordelen), en de gezondheidsbewaker.
- **c. Voor het projectbestand: vergelijk oud tegen nieuw.** Ledenwijzigingen, rolwijzigingen
  en dienstwijzigingen zijn geen aanroep maar een verschil. Op de opslagweg staan beide versies
  ter beschikking. Eén vergelijkingsfunctie die een lijst gebeurtenissen teruggeeft, dekt alle
  gebeurtenissen die uit het projectbestand komen in één keer, ook toekomstige. Dit is de enige
  plek waar declaratief werk echt loont.
- **b. Alleen waar de dienst het weet.** `notices_for` in de `ApprovalSpec` is er al: de dienst
  schrijft de zin die de aanvrager te lezen krijgt, want alleen de dienst kent het gevolg. Als
  een dienst een eigen gebeurtenis heeft met een tekst die alleen hij kan schrijven, hoort dat
  op dezelfde manier. Dat is een uitbreiding van het dienstcontract en het is klein te houden.

**Wat hier NIET moet gebeuren.** Geen abstractie bouwen die alle drie de vormen achter één
gezicht verstopt. Ze zijn wezenlijk verschillend (een aanroep, een vergelijking, een
declaratie) en één gezicht maakt alleen dat je bij het lezen niet meer ziet welke het is.

## 2. Betrouwbaar afleveren

**De outbox.** De gebeurtenis wordt geschreven in **dezelfde transactie** als de handeling die
hem veroorzaakte. Faalt de handeling, dan is er geen gebeurtenis. Slaagt de handeling, dan
staat de gebeurtenis er, ook als de aflevering later stukloopt. Dat is de enige manier om
"twee dingen die allebei of geen van beide moeten gebeuren" te krijgen zonder een tweede
systeem.

Dat werkt hier omdat de meeste handelingen al door Postgres gaan: de takenrij, de runs, de
markeringen. Voor de handelingen die naar git schrijven (het projectbestand) is het niet
atomair, en dat moet je gewoon opschrijven: de commit slaagt en de gebeurtenis faalt is een
denkbare toestand. De juiste volgorde is dan: eerst committen naar git, dan de gebeurtenis
schrijven, want een gemiste melding is minder erg dan een melding over iets wat niet gebeurd is.

**Wie hem leegdrinkt.** Twee kandidaten en de keuze is niet vanzelfsprekend.

*De bestaande takenwerker* (`opi/core/task_worker.py`) draait al, heeft al een hoofdlus, een
hartslag, een herstellus voor vastgelopen taken en een opruimlus. Hij aanhaken is het minste
werk. Maar: hij verwerkt precies één taak tegelijk en de taken zijn zwaar (een deployment
duurt minuten). Een melding zou dan achter een uitrol in de wachtrij staan. Dat is niet
acceptabel voor iets wat binnen seconden hoort aan te komen.

*Een eigen planner in de lifespan van `server.py`* staat naast de zes die er al staan (de
backupplanner, de stemmer, de reconciliatie, de consolereaper, de logbewaker en de
slaapstandsweeper, `opi/server.py:214` en verder). Dat is het bekende patroon van dit
codebestand, elke planner is een tiental regels, en hij is onafhankelijk in te stellen en uit
te zetten.

**Aanbeveling: een eigen planner.** Dezelfde vorm als de bestaande zes, met een korte tik (een
paar seconden) en een partiële index op wat nog niet is afgeleverd. Voordeel boven de
takenwerker: hij loopt niet vast achter een uitrol, en hij is los uit te schakelen zonder de
takenrij te raken.

**Let op de valkuil die dit codebestand zelf al heeft gedocumenteerd.** Een planner in de
lifespan draait per proces. Draaien er meerdere OPI-processen, dan drinken ze allemaal
dezelfde outbox leeg en wordt elke melding meerdere keren afgeleverd. De takenrij heeft dit
opgelost met een claimmechanisme (`claim_next_task`, `async_task_service.py:182`) plus een
`instance_id`. De outboxplanner moet hetzelfde doen: claim met `FOR UPDATE SKIP LOCKED` of het
equivalent daarvan dat de takenrij al gebruikt. Dit is geen theoretisch punt; de
websocket-router noteert al dat zijn eigen limieten per werker gelden.

**Opnieuw proberen.** Per aflevering (niet per gebeurtenis) een pogingsteller en een
`volgende_poging_op`. Exponentieel oplopend met een plafond, in de orde van vijf minuten. Na
een vast aantal pogingen: opgeven, de aflevering markeren als mislukt, en dat zelf als
gebeurtenis vastleggen voor de platformbeheerder. Een kanaal dat structureel niets aanneemt,
hoort een storing te zijn en geen stilte.

**Idempotentie.** Twee lagen, want ze dekken verschillende fouten:

- **Bij het aanleggen**: een `dedup_sleutel` met een uniciteitsgrendel (zie punt 3). Twee keer
  dezelfde gebeurtenis aanleggen levert één rij op.
- **Bij het afleveren**: een aflevering gaat van `wachtend` naar `bezig` naar
  `afgeleverd`/`mislukt`, en de overgang naar `bezig` is de claim. Een werker die halverwege
  omvalt, laat een aflevering in `bezig` staan; die wordt na een tijdsdrempel teruggezet, net
  zoals `recover_stale_tasks` dat voor taken doet. Dat betekent dat een bericht in het
  ergste geval twee keer aankomt in plaats van nul keer, en dat is de goede kant om op te falen.

**Wat als een kanaal plat ligt.** Het postvak is geen kanaal in deze zin: die rij staat er al,
want die is de gebeurtenis. Alleen mail en Mattermost zijn afleveringen die kunnen falen, en
die stapelen zich op in de outbox. Als de mailrelay een uur weg is, komen na dat uur alle
berichten alsnog. Voor de gebruiker betekent dat een stapel; dat is een reden om per persoon
per tijdvak samen te vatten (zie deel 3, "E-mail").

## 3. Ontdubbelen en samenvoegen

Dit is niet één probleem maar drie, en ze vragen om verschillende antwoorden.

**a. Dezelfde gebeurtenis wordt twee keer waargenomen.** De gezondheidsbewaker draait elke
ronde opnieuw en ziet dezelfde `CrashLoopBackOff`. Antwoord: een **dedupsleutel** op de
gebeurtenis, met een uniciteitsgrendel plus een venster. De sleutel is de gebeurtenis zonder
zijn vluchtige delen: type + onderwerp + de kern van de melding. Dit is precies wat
`signature()` in `log_watcher.py:312` doet, en die functie is beproefd: hij strookt
tijdstempels, IP-adressen, gekoppelde identifiers en losse getallen weg tot een stabiele
sleutel van maximaal 120 tekens. **Overnemen, en de toestand in Postgres zetten in plaats van
in het geheugen.** Dat laatste is de fout die in de bestaande planner zit (deel 1, paragraaf 9).

**b. Twintig gebeurtenissen over hetzelfde ding.** Een deployment die twintig keer herstart.
Dat zijn twintig echte gebeurtenissen en het is één melding. Antwoord: een **teller op de
bestaande melding** in plaats van een nieuwe rij. Als binnen het venster dezelfde dedupsleutel
terugkomt, hoog dan `aantal` op en zet `laatst_gezien_op` bij, en laat de melding ongelezen
staan (of zet hem terug op ongelezen als hij al gelezen was). Dit is hoe
`InterpretedEvent.count` (`event_interpreter.py:27`) het vandaag al doet binnen één weergave;
hier is het hetzelfde over de tijd.

**c. Verschillende gebeurtenissen over hetzelfde onderwerp.** Een uitrol die faalt, gevolgd door
een automatische stemming, gevolgd door een geslaagde uitrol. Drie gebeurtenissen, één verhaal.
Antwoord: een **draadsleutel** (`draad` in het datamodel), die het ONDERWERP benoemt en niet de
gebeurtenis: `deployment:<project>/<deployment>`, `aanvraag:<project>/<dienst>/<sleutel>`. Het
postvak groepeert op draad. Dat is wat GitHub met een issue-draad doet, en het is de reden dat
een druk project daar leesbaar blijft.

**En de belangrijkste keuze: het venster.** Te kort en je krijgt herhaling, te lang en een
echte tweede storing verdwijnt in de eerste. De logbewaker staat op zes uur, en dat is voor
ops-alarmen verdedigbaar. Voor een postvak is dat te lang: als je een melding om negen uur
leest en om elf uur gaat hetzelfde weer mis, hoor je dat te zien. **Voorstel: het venster loopt
tot de melding gelezen is, met een plafond.** Ongelezen plus dezelfde sleutel betekent
optellen; gelezen betekent een nieuwe melding. Dat is precies hoe een mens erover denkt en het
vraagt geen instelbare duur.

## 4. De verhouding tot het audittrail

**Kort antwoord: twee dingen, en ze mogen best in één tabel beginnen.**

De vraag is of "wat is er gebeurd" (bewijs, onveranderlijk, compleet) en "wat moet jij weten"
(persoonlijk, wegklikbaar) hetzelfde zijn. In het aanbevolen model zijn ze dat al **niet**,
want het zijn twee tabellen: de gebeurtenis is onveranderlijk en compleet, de ontvangerrij is
persoonlijk en wegklikbaar. Wat de gebruiker wegklikt is zijn rij, niet de gebeurtenis.

**Wat er vandaag aan audittrail ligt**, want dat is minder dan je zou denken:

| Wat | Waar | Vorm |
|---|---|---|
| Wijzigingen aan een projectbestand | de git-geschiedenis van `zad-projects` | commits, compleet, onveranderlijk |
| Goedkeuringsoordelen | het `history`-blok in het projectbestand | wie, wanneer, welk oordeel, welke notitie |
| Automatische stemming | het `history`-blok bij `resources` in het projectbestand | tijdstip, bron, reden |
| Domeinclaims | de logger `opi.audit.subdomain` (`subdomain_registry.py:211` e.v.) | logregels, dus zo lang als de logretentie |
| Runs | de tabel `runs`, met `started_by`, `started_at`, `ended_by` | rijen, niet opgeruimd |
| Taken | de tabel `async_tasks` | rijen, **na één uur weg** |

Er is dus **geen audittabel**. `plans/bio2-compliance-analysis.md` benoemt dat zelf als een
HIGH-bevinding onder A8.15: "Logging exists but no structured audit trail (who did what,
when)", en onder A5.28: "No forensic logging or tamper-proof audit trail".

**De BIO-kant, kort en concreet.** De relevante controls uit `plans/bio2-compliance-analysis.md`:

- **A8.15 (logging)**: een gebeurtenistabel met wie, wat, wanneer en waarover is precies wat
  daar ontbreekt. Meldingen leveren dat als bijvangst, mits de gebeurtenis de actor draagt.
- **A8.16 (monitoring)**: het gaat over waarnemen, en dat doet Prometheus. Meldingen raken dit
  niet.
- **A5.24 (incidentbeheer)**: nu genoteerd als "errors logged but no escalation/notification".
  Dit is letterlijk wat hier gebouwd wordt.
- **A5.28 (bewijs)**: vraagt om onveranderlijkheid. Dat is een ontwerpregel voor de
  gebeurtenistabel: alleen invoegen, nooit bijwerken, nooit verwijderen binnen de bewaartermijn.

**De privacykant, ook kort.** Het Logboek Dataverwerkingen (Logius, alleen werkversies, nog
niet vastgesteld en niet op de lijst van het Forum Standaardisatie) gaat over het loggen van
verwerkingen van persoonsgegevens van BETROKKENEN, met een `dpl.core.data_subject_id` die
bij voorkeur gepseudonimiseerd is. **Dat is hier niet aan de orde en dat moet expliciet
opgeschreven worden, want anders wordt het per ongeluk toch gebouwd.** ZAD verwerkt geen
persoonsgegevens van burgers; de personen in dit systeem zijn de gebruikers van het portaal
zelf, en de gebeurtenissen gaan over infrastructuur.

Wat wél geldt, en dat is de enige echte eis die eruit volgt:

- **Een meldingsrij is een persoonsgegeven.** "Deze persoon kreeg op dat tijdstip die melding"
  gaat over een identificeerbaar persoon. Dus: een bewaartermijn (zie het datamodel), en bij
  het verwijderen van een gebruiker gaan zijn ontvangerrijen mee (de gebeurtenissen blijven,
  want die gaan over het platform en niet over hem).
- **Geen persoonsgegevens in de context-attributen.** Dit is de regel uit het
  CloudEvents-profiel en hij is hier praktisch: `subject` draagt de projectnaam en niet het
  e-mailadres van de actor. De actor hoort in de payload, niet in het routeringsveld.
- **Een melding die naar buiten gaat, verlaat ons vertrouwensgebied.** Dat is de reden dat een
  mail zo min mogelijk inhoud draagt; zie deel 3.

**De aanbeveling voor het audittrail zelf: doe het niet in deze PR-serie, maar bouw de
gebeurtenistabel zo dat hij het kan worden.** Concreet: elke gebeurtenis draagt de actor, het
tijdstip en het onderwerp; de tabel is alleen-invoegen; de bewaartermijn van de gebeurtenissen
is langer dan die van de ontvangerrijen. Dan is "maak er een audittrail van" later een
kwestie van meer gebeurtenissen aanleggen en een langere bewaartermijn, en niet van een tweede
tabel.

## 5. Het datamodel

In de stijl die er ligt: SQLAlchemy-modellen onder `opi/services/persistence/`, op de `Base`
uit `opi/core/db.py`, geregistreerd in `opi/services/persistence/__init__.py`, en een migratie
onder `opi/migrations/versions/`.

**Alle namen hieronder zijn een voorstel.** De bestaande tabellen heten `async_tasks`, `runs`,
`users`, `marked_for_deletion` en `subdomain_registry`, dus Engels en meervoud; die lijn houd
ik aan.

### `notification_events` (voorstel): de gebeurtenis

Onveranderlijk. Alleen invoegen. De kolomnamen volgen het NL GOV-profiel waar het profiel iets
zegt.

```python
class NotificationEvent(Base):
    """Een gebeurtenis op het platform. Onveranderlijk: alleen invoegen."""

    __tablename__ = "notification_events"

    # CloudEvents: id, source, specversion, type, subject, time
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, server_default=text("gen_random_uuid()"))
    source: Mapped[str] = mapped_column(String(255), nullable=False)      # urn:nld:...:systeem:zad-<cluster>
    specversion: Mapped[str] = mapped_column(String(8), nullable=False, server_default=text("'1.0'"))
    type: Mapped[str] = mapped_column(String(128), nullable=False)        # nl.zad.deployment.mislukt.v1
    subject: Mapped[str | None] = mapped_column(String(255))              # <project>/<deployment>
    time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )

    # Van ons, want het profiel zegt er niets over
    category: Mapped[str] = mapped_column(String(32), nullable=False)     # een van de twaalf typen
    severity: Mapped[str] = mapped_column(String(16), nullable=False)     # informational|actionable|outage
    cluster: Mapped[str] = mapped_column(String(63), nullable=False)
    project: Mapped[str | None] = mapped_column(String(63))
    deployment: Mapped[str | None] = mapped_column(String(63))
    actor: Mapped[str | None] = mapped_column(String(255))                # e-mailadres, of NULL bij het platform
    thread_key: Mapped[str | None] = mapped_column(String(255))           # deployment:<project>/<deployment>
    dedup_key: Mapped[str] = mapped_column(String(160), nullable=False)   # signature()-vorm, zie punt 3
    occurrences: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    data: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'"))

    __table_args__ = (
        Index("idx_notification_events_dedup", "dedup_key", "time"),
        Index("idx_notification_events_project", text("project"), text("time DESC")),
        Index("idx_notification_events_thread", text("thread_key"), text("time DESC")),
    )
```

**Toelichting bij de keuzes die niet vanzelf spreken:**

- `data` is JSONB en niet een set kolommen, want wat er in een gebeurtenis staat verschilt per
  type. Dat is dezelfde afweging als `spec` in `runs` en `payload` in `async_tasks`, dus het is
  hier het huispatroon en geen nieuwe vondst.
- `occurrences` en `last_seen_at` op de gebeurtenis en niet op de ontvangerrij: het is één
  gebeurtenis die vaker voorkwam, niet vaker bezorgd.
- **Geen uniciteitsgrendel op `dedup_key` alleen.** Dat zou betekenen dat dezelfde storing over
  een half jaar niet opnieuw kan optreden. De grendel hoort te gelden binnen het venster, en dat
  is geen constraint maar een `INSERT ... ON CONFLICT`-achtige stap in de code die eerst zoekt
  binnen het venster. De index `idx_notification_events_dedup` bedient precies die zoekvraag.
- `severity` gebruikt de drie waarden van `EventSeverity` uit `event_interpreter.py:18`, maar
  met `noise` eruit: wat ruis is, wordt geen gebeurtenis.

### `notification_deliveries` (voorstel): de rij per persoon

```python
class NotificationDelivery(Base):
    """Wat er voor EEN persoon is neergelegd, met leesstatus en reden."""

    __tablename__ = "notification_deliveries"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, server_default=text("gen_random_uuid()"))
    event_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("notification_events.id", ondelete="CASCADE"), nullable=False
    )
    recipient: Mapped[str] = mapped_column(String(255), nullable=False)   # e-mailadres, kleine letters
    reason: Mapped[str] = mapped_column(String(64), nullable=False)       # project-admin|project-member|actor|approver|platform-admin
    created_at: Mapped[datetime] = mapped_column(
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
    )
```

**De partiële index `idx_notification_deliveries_unread` is de belangrijkste regel van dit hele
datamodel.** Dat is de index waarop de teller in de kop van elke pagina draait. Zonder hem is
die teller een tabelscan bij elke paginaweergave; met hem is het een indexlezing over een index
die alleen de ongelezen rijen bevat, en die is klein omdat mensen hun postvak leegmaken.
Dezelfde vorm als `idx_runs_active` in `opi/services/persistence/runs.py`, dus het patroon ligt
er al.

`reason` is de kolom die richting B niet kan hebben: hij bewaart WAAROM deze persoon deze
melding kreeg, op het moment dat dat gold. Vandaar de vaste waardenlijst en niet een vrije
tekst.

### `notification_channel_deliveries` (voorstel): de outbox

Apart van de ontvangerrij, want een persoon kan één melding via twee kanalen krijgen en die
kunnen los van elkaar slagen of falen.

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

De uniciteitsgrendel op `(delivery_id, channel)` is de idempotentie bij het aanleggen: twee
keer dezelfde aflevering plannen levert één rij op. `claimed_by` en `claimed_at` zijn de
claim die meerdere OPI-processen uit elkaar houdt, in dezelfde vorm die `async_tasks` al
gebruikt (`instance_id` in `AsyncTaskService.__init__`). De partiële index op
`next_attempt_at` is wat de planner elke tik bevraagt, en hij is klein want alleen wachtende
rijen staan erin.

### `notification_preferences` (voorstel): de voorkeuren

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

**Afwezigheid betekent de standaard van de rol, en niet "uit".** Dat is een echte keuze: het
alternatief is bij het aanmaken van een gebruiker 12 typen keer 3 kanalen aan rijen schrijven,
en dan is een wijziging in de standaarden niet meer door te voeren bij bestaande gebruikers.
Zo staat er alleen in de tabel wat iemand bewust anders heeft gezet.

### Bewaartermijn en wie hem opruimt

| Tabel | Voorstel | Waarom |
|---|---|---|
| `notification_events` | **1 jaar** | lang genoeg om "wat is er in het laatste kwartaal gebeurd" te beantwoorden, en het is de tabel die later een audittrail kan worden |
| `notification_deliveries` | **90 dagen na lezen; ongelezen blijven staan tot de gebeurtenis weg is** | een melding die je nooit las mag niet verdwijnen; een gelezen melding is klaar |
| `notification_channel_deliveries` | **30 dagen na `sent_at`** | dit is uitsluitend werkadministratie |
| `notification_preferences` | **niet opruimen**, wel mee met de gebruiker | een voorkeur is een instelling |

**Wie hem opruimt: de outboxplanner, in een tweede lus.** Precies zoals de takenwerker naast
zijn hoofdlus een `_cleanup_loop` heeft (`task_worker.py:420`) en de backupplanner zijn
retentiesweep één keer per dag draait (`backup_scheduler.py:195`). Eén keer per dag, na
kantooruren, en de getallen instelbaar via `settings` met dezelfde naamgeving als de rest
(`NOTIFICATIONS_EVENT_RETENTION_DAYS`, voorstel).

**Bij het verwijderen van een gebruiker**: zijn `notification_deliveries`,
`notification_channel_deliveries` en `notification_preferences` gaan mee. De
`notification_events` blijven, want die gaan over het platform. Dat hoort in
`UserAdminService.delete_user` (`opi/services/user_admin_service.py:65`) en het is een regel
die nu opgeschreven moet worden, want anders wordt hij vergeten.

### De migratieweg

De vier modules komen onder `opi/services/persistence/` en worden geïmporteerd in
`opi/services/persistence/__init__.py`, want dat is wat ze op `Base.metadata` zet en dus
zichtbaar maakt voor Alembic (`include_orm_object` in `opi/core/db.py:112` beperkt
autogenerate tot precies die tabellen).

De migratie wordt `opi/migrations/versions/005_add_notifications.py`, met `down_revision = "004"`.

**Let op een verschil met wat de opdracht aanneemt.** De opdracht zegt "autogenerate is gericht
op de ORM-modellen", en dat klopt. Maar de vier bestaande migraties doen het niet zo: ze voeren
een `op.execute()` uit op een SQL-constante uit `opi/core/*_schema.py` (zie
`004_add_runs.py`, dat `RUNS_TABLE_SQL` uitvoert). Dat is een overblijfsel van de tijd voor de
ORM en het is bewust gedocumenteerd in `opi/core/db.py:1`.

Voor een nieuwe tabel is er geen SQL-constante om te erven, dus hier is de keuze vrij, en dat
is een beslissing die iemand moet nemen:

- **Autogenerate en de ORM als bron.** Schoner, en het is de richting die `db.py` zelf
  beschrijft ("makes it the schema-as-code source of truth"). Nadeel: het wijkt af van de vier
  migraties die er liggen.
- **Een `NOTIFICATIONS_TABLE_SQL` en `op.execute()`.** Consistent met wat er ligt. Nadeel: het
  is twee keer dezelfde waarheid schrijven, en dat is precies waar `db.py` vanaf wilde.

**Mijn aanbeveling: autogenerate.** Deze tabellen hebben geen historie om te dragen, de
schema-driftcontrole (`scripts/check_orm_schema.py`) bewaakt het al, en het is de kant waar het
codebestand naartoe beweegt. Wel de gegenereerde migratie nalezen: autogenerate zet partiële
indexen niet altijd goed neer, en die zijn hier niet decoratief.
