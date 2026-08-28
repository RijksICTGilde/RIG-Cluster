# Het beheerdeel van ZAD: het rollenmodel, het overzicht en de grensregel

**Geschreven op**: 28 augustus 2026, tegen commit `d32fb07e` op de tak
`het-beheerdeel-van-zad-rollen-overzicht-en-wat-een`. Dit is deel 2 van twee; deel 1 (de
gemeten inventarisatie) staat in `plans/beheer-in-zad-inventarisatie.md`.

Dit document beantwoordt drie vragen en sluit af met de fasering:

1. **Welke scheiding tussen beheerders verdient het om te bestaan?**
2. **Waar hoort een gebeurtenis thuis: postvak, overzicht of opskanaal?** En hoe formuleer je
   die grens zo dat twee mensen er onafhankelijk hetzelfde mee uitkomen?
3. **Hoe ziet de beheerdersstartpagina eruit, en hoe verhoudt hij zich tot de vijf pagina's
   die er al zijn?**

**Alle namen van rollen, paden en pagina's hieronder zijn een voorstel**, tenzij er een
codeanker bij staat.

De correctie op de meldingen zelf (de standaarden per rol, het voorkeurenscherm, de
verversingsweg) staat niet hier maar in `plans/meldingen-plan-van-aanpak.md`, met een
wijzigingslijst onderaan dat document. Twee documenten die elkaar tegenspreken zijn erger dan
één document dat is bijgewerkt.

---

## Deel 2: het rollenmodel

### Waar we vandaan komen, in drie regels

Er is één rol, plat: `is_platform_admin` (`opi/services/user_service.py:279-281`). Op
productie heeft hij twee dragers. Hij wordt op vier plekken uitgelezen, en twee daarvan geven
hem toegang tot elk project en de rol `admin` in elk project
(`opi/services/project_authorization.py:42` en `:62`), wat op 36 aanroeppunten doorwerkt. Zie
deel 1, paragraaf 2.

### Wat de BIO hier wel en niet afdwingt

Eerst een verantwoording van de bron. De opdracht verwijst naar een skill `bio`. **Die bestaat
niet in deze omgeving**: er is geen `skills`-map met dat onderwerp te vinden. Wat hieronder
staat komt daarom uit de BIO-documenten die in deze repository staan, en niet uit mijn geheugen:

- `features/bio-network-access-no-vpn-compliance.md` (BIO2 v1.3 definitief, 9 januari 2026,
  gepubliceerd in de Staatscourant 5 maart 2026; scope ZAD/OPI op `odcn-production`);
- `plans/bio2-compliance-analysis.md`;
- `plans/technische-review-bio-en-nora-bevindingen.md`.

Uit die documenten, en alleen daaruit:

**Wat de BIO2 níet doet.** Ze schrijft geen techniek voor. Ze is technologie-neutraal en
risicogestuurd, en eist "aantoonbare, risicogestuurde toegangsscheiding" in plaats van een
bepaalde oplossing (`features/bio-network-access-no-vpn-compliance.md`, "Oordeel"). Er is dus
**geen BIO-eis die zegt hoeveel beheerdersrollen ZAD moet hebben.** Wie een tweede rol
verdedigt met "de BIO wil dat", verdedigt hem verkeerd.

**Wat de BIO2 wél doet, en dit is de enige eis die hier scherp bijt.** De organisatorische
borging noemt met zoveel woorden: "**Speciale bevoegdheden minimaal per kwartaal beoordelen
(8.02.01)**" (`features/bio-network-access-no-vpn-compliance.md:146`). Een beoordeling
veronderstelt een lijst. Vandaag is die lijst:

- niet in de applicatie zichtbaar (`/admin/users` heeft geen rolkolom, deel 1 paragraaf 1);
- niet in de applicatie te wijzigen (het is een ConfigMap plus een herstart);
- deels hardgecodeerd (`opi/core/startup.py:559-562`);
- en er is een **tweede** verzameling speciale bevoegdheden, `ADMIN_API_KEY`, die niemand een
  rol noemt en waarvan niet is vast te stellen wie hem heeft (deel 1, paragraaf 2).

**De BIO-eis die hier bijt gaat dus niet over het aantal rollen maar over de zichtbaarheid en
de herleidbaarheid van de bevoegdheid die er is.** Dat is een belangrijk onderscheid, want het
maakt de goedkoopste optie ook de meest conforme.

Een tweede punt uit dezelfde bron, zwakker maar wel relevant: bij de compenserende maatregelen
staat "Detectie/herleidbaarheid: Logging (8.15) + monitoring (8.16)". De 36 doorwerkingen van
`is_platform_admin` loggen op DEBUG, en `LOG_TO_FILE=false` op productie. Er is dus geen spoor
van beheerderstoegang tot een project. Dat is geen harde overtreding van een control die ik in
deze documenten kan aanwijzen, maar het is wel precies wat 8.15 en 8.16 beogen.

**Functiescheiding.** Ik heb in de drie BIO-documenten in deze repository geen control
gevonden die hier functiescheiding tussen beheerderstaken afdwingt. `plans/bio2-compliance-analysis.md:29`
noemt A5.15 (Access Control) als "Good". Dat is dus **geen** argument voor model C hieronder,
en ik voer het ook niet zo op. Er is bovendien een praktisch bezwaar dat zwaarder weegt dan
elk papier: **met twee beheerders is elke functiescheiding een scheiding van dezelfde twee
mensen.** Papieren scheiding is slechter dan geen scheiding, want hij suggereert een controle
die er niet is.

### De drie modellen naast elkaar

#### Model A: één rol houden, en het probleem oplossen waar het zit

`is_platform_admin` blijft wat hij is. Het probleem dat de opdrachtgever aankaart (te veel
meldingen) wordt opgelost in het overzicht (deel 3) en in de standaardentabel
(`plans/meldingen-plan-van-aanpak.md`).

| | |
|---|---|
| **Kosten** | nul in het autorisatiepad |
| **Voor** | het probleem is een verdelingsprobleem en een schermprobleem, niet een rollenprobleem. Een rollenmodel voor twee mensen is overhead. KISS en YAGNI wijzen allebei deze kant op. |
| **Tegen** | de BIO-eis 8.02.01 blijft onvervulbaar zolang de lijst nergens staat. En de 36 doorwerkingen blijven zonder spoor. |
| **Maar** | allebei die bezwaren gaan over **zichtbaarheid**, niet over **rollen**, en zijn dus binnen dit model op te lossen. |

#### Model B: een leesrol naast de beheerrol

Een tweede verzameling, voorstel `platform-viewer`, met een eigen grendel
(voorstel: `require_platform_viewer`) die ook waar is voor een `platform-admin`.

| | |
|---|---|
| **Kosten** | een tweede set in `UserService`, een tweede instelling, een tweede grendel, en per route de keuze welke van de twee. Zestien routes (deel 1, paragraaf 1). Plus de vraag wat de leesrol met projecten mag. |
| **Voor** | het beheerdersoverzicht wordt deelbaar met iemand die dienst heeft, zonder hem de sleutel tot 47 projecten te geven. Dat is een echt scenario: het overzicht uit deel 3 is precies een pagina die je aan een meekijker wilt kunnen laten zien. |
| **Tegen** | vandaag zijn er nul kandidaten voor die rol. Een rol zonder dragers bouwen is YAGNI in zijn zuiverste vorm, en het is een rol die bij elke nieuwe route opnieuw een beslissing kost. |
| **Wanneer wel** | zodra er een tweede soort mens is die het overzicht moet zien. Dat is een organisatievraag, geen bouwvraag. |

#### Model C: een taakgerichte scheiding

Wie aanvragen afhandelt is niet wie gebruikers beheert. **De naad hiervoor ligt er al**:
`ApproverScope` kent `PLATFORM_ADMIN`, `PROJECT_ADMIN` en `PROJECT_MEMBER`
(`opi/services/catalog/approval.py:45-56`).

| | |
|---|---|
| **Kosten** | het duurst van de drie. Per beheerpagina een eigen recht, per recht een lijst, en de vraag wie wat krijgt bij elke nieuwe pagina opnieuw. |
| **Voor** | de goedkeurder van een domein hoeft geen gebruikers te kunnen verwijderen. Dat is op zichzelf een gezonde gedachte. |
| **Tegen** | twee dragers (zie boven). En het bestaande naadje doet niets: het veld `approver` wordt **nergens uitgelezen** (nul treffers op `.approver` in `opi/`), dus twee van de drie waarden zijn dood. Een scheiding invoeren op een naad die zelf nog nooit iets heeft gedragen, is twee onbewezen dingen tegelijk. |
| **Wanneer wel** | als er ooit goedkeuringen komen die bij een PROJECTbeheerder horen. Dan wordt `spec.approver` uitlezen de eerste stap, en dat is een kleine stap die dan waarde heeft. |

### De aanbeveling: model A, plus twee gerichte ingrepen die geen nieuwe rol zijn

**Houd één platformbeheerdersrol.** Model B en C lossen geen probleem op dat we vandaag
hebben, en ze kosten allebei blijvend werk bij elke nieuwe route. Wat er wel moet gebeuren
zijn twee dingen die geen van beide een rol toevoegen.

**Ingreep 1: maak de bevoegdheid zichtbaar en beheerbaar.**

- Een kolom "platformbeheerder" op `/admin/users`, en een knop om hem te zetten en weg te
  halen. Dat vraagt één kolom op de `users`-tabel (`opi/services/persistence/users.py`) en een
  bron die de configuratie aanvult in plaats van vervangt: de ConfigMap blijft de noodingang
  (anders sluit je jezelf buiten), de database wordt de gewone weg.
- Daarmee is 8.02.01 in één scherm te vervullen, wordt "iemand is platformbeheerder geworden"
  een handeling met een actor en een tijdstip in plaats van een uitrol, en verdwijnt de regel
  "**bestaat nog niet**" uit de eventcatalogus van RC-148
  (`plans/meldingen-inventarisatie.md`, paragraaf 7).
- **En zet `ADMIN_API_KEY` op dezelfde pagina, of haal hem weg.** Vandaag is hij op productie
  niet gezet en zijn de zeven endpoints erachter 501 (deel 1, paragraaf 2). Er zijn twee
  eerlijke uitkomsten: die endpoints achter `require_platform_admin` zetten en de sleutel
  laten vallen, of de sleutel bewust configureren en zijn houders opschrijven. Wat er nu is,
  is de derde: een beheerdersweg die niemand kan gebruiken en die in de documentatie wel als
  werkwijze staat.

**Ingreep 2: splits de doorwerking naar projecten in lezen en schrijven.** Dat is de volgende
paragraaf, want het is de expliciete vraag uit de opdracht.

### Hoort een beheerder standaard bij alle projecten te kunnen?

**Lezen ja. Schrijven nee, en niet stilzwijgend.**

**Waarom lezen wel.** Een platformbeheerder die niet in een projectbestand mag kijken kan zijn
werk niet doen: de helft van elk incident begint met een projectbestand lezen. En de gegevens
zijn niet geheim voor hem in enige zinnige betekenis: hij heeft clustertoegang, dus wat hij
niet in ZAD ziet, ziet hij met `kubectl`. Leestoegang afsluiten levert dus geen beveiliging op,
alleen omweg.

**Waarom schrijven niet standaard.** Drie redenen, oplopend in gewicht:

1. **De rol zegt niets over het project.** `get_user_role_for_project` geeft `admin` terug op
   grond van een lijst die niets met dat project te maken heeft
   (`opi/services/project_authorization.py:59-62`). Voor de projecteigenaar is dat niet te
   zien: het teamblok toont zijn eigen leden, en de beheerder staat daar niet tussen.
2. **De projectsleutel ligt op de pagina.** Het paneel met de `api-key` staat achter
   `user_role in ["admin","owner"]` (`opi/templates_lotc/bg/project-tabs.html.j2:163`, veld op
   `:177`) en de API-lijst geeft hem mee onder dezelfde voorwaarde
   (`opi/api/v2/router.py:1177`). Die sleutel draagt zelf geen rol en opent elke muterende
   route van dat project (`opi/services/project_authorization.py:24-27`). Een beheerder kan
   hem dus van elk van de 47 projectpagina's meenemen, en daarna is de grens weg.
3. **Er is geen spoor.** Zie deel 1, paragraaf 2.

**Waarom "niet stilzwijgend" belangrijker is dan "niet".** Wie overal bij kan, is ook overal
belanghebbende, en dat is precies waarom de RC-148-standaard op "alles" uitkwam: als de
beheerder bij elk project hoort, hoort elke projectmelding bij hem. Het omgekeerde geldt ook:
**maak de toegang tot een project een handeling, en de meldingsvraag lost zichzelf voor een
groot deel op.** Een beheerder die beheer heeft overgenomen van drie projecten is
belanghebbende bij drie projecten, niet bij 47.

**Het mechanisme, en het is klein.** Verander niets aan `is_user_authorized_for_project`
(lezen blijft), en laat `get_user_role_for_project` voor een platformbeheerder die **niet in de
`users` van dat project staat** een andere waarde teruggeven dan `admin`.

Dat werkt door zonder dat er ergens anders iets hoeft te veranderen, en dat is gemeten:

| Laag | Aantal grendels | Vorm |
|---|---|---|
| Sjablonen | **13** | `{% if user_role in ["admin", "owner"] %}`, in `opi/templates_lotc/bg/project-tabs.html.j2` (10: `:103`, `:143`, `:163`, `:347`, `:380`, `:405`, `:452`, `:789`, `:822`, `:898`), plus `opi/templates_lotc/bg/_pending-rollout.html.j2:34`, `opi/templates_lotc/bg/_section-deployments.html.j2:87` en `opi/templates_lotc/bg/_deployment-actions.html.j2:28` |
| Webroutes | **9** | `if user_role not in ["admin", "owner"]` in `opi/web/router.py` |
| Bewerkgrendel | **1** | `opi/web/project_edit_security.py:46`, tegen `PROJECT_EDIT_ROLES` |
| API | **1** | `opi/api/v2/router.py:1177`, de sleutel in de projectlijst |

**Alle 24 zijn positieve lijsten.** Er is geen enkele controle van de vorm "als de rol niet X
is", dus een waarde die niet in `["admin","owner"]` staat valt overal door naar geen rechten.
Dat is nagelopen: `grep` op `user_role` levert in de sjablonen dertien keer `in [...]` en nul
keer een ontkenning, en in `opi/web/router.py` negen keer `not in ["admin", "owner"]` als
weigering.

**Welke waarde.** Twee opties, en ik heb een voorkeur:

- **`member`** (bestaat al in het schema: `admin`, `owner`, `member`, `developer`,
  `opi/schemas/project_v2.json`). Voordeel: geen enkele nieuwe waarde. Nadeel: de beheerder is
  op het scherm niet te onderscheiden van een echt projectlid, en dat is precies de
  verwarring die we wilden weghalen.
- **`platform-viewer` (voorstel)**, een waarde die alleen uit deze functie komt en nooit in
  een projectbestand staat. Voordeel: op het scherm staat wat er aan de hand is ("je kijkt mee
  als platformbeheerder"), en het is te tellen. Nadeel: een waarde erbij die het schema niet
  kent, dus elke plek die de rol toont moet hem kunnen weergeven.

**Aanbeveling: `platform-viewer`.** De 24 grendels vangen hem allemaal op als "geen rechten",
en het verschil tussen meekijken en meedoen hoort zichtbaar te zijn.

**En de handeling erbij.** Een knop op de projectpagina, voorstel "Beheer overnemen", die de
beheerder tijdelijk `admin` geeft op dat project. Wat die knop moet doen:

1. een reden vragen (één tekstveld, verplicht);
2. een gebeurtenis aanmaken van type `beheer` met `actor` = de beheerder, bezorgd aan de
   projectbeheerders met `reason = "project-admin"`, en aan de andere platformbeheerders met
   `reason = "platform-admin"`;
3. na een termijn (voorstel: 24 uur) vanzelf vervallen.

Dat is de enige plek in dit hele voorstel waar nieuwe toestand nodig is, en het is één tabel
met vier kolommen (beheerder, project, reden, vervaltijd). Zonder die knop is het voorstel
niet af, want dan is de enige weg terug naar schrijfrechten "zet jezelf in het projectbestand",
en dat is een stillere wijziging dan wat we wegnemen.

### Wat dit voorstel NIET afsluit, en dat hoort erbij

Eerlijk zijn over de gaten, want ze zijn er:

- **Wie de projectsleutel al heeft, houdt alles.** De sleutel draagt geen rol. Dit voorstel
  voorkomt dat een beheerder er een nieuwe van een pagina plukt; het maakt bestaande sleutels
  niet zwakker.
- **De logstroom blijft open.** `opi/api/logs_websocket_router.py:355` toetst alleen
  autorisatie en niet de rol, dus een beheerder blijft bij de logs van elk project kunnen. Dat
  is een bewuste keuze: logs lezen is de kern van eerstelijnswerk. Als dat anders moet, is het
  een eigen beslissing en niet een gevolg van deze.
- **`kubectl` blijft `kubectl`.** Wie clustertoegang heeft komt overal bij. Dit voorstel gaat
  over wat ZAD toont en toestaat, niet over wat het cluster toestaat.
- **De zeven `ADMIN_API_KEY`-endpoints staan hier los van.** Zie ingreep 1.

---

## Deel 3: de grensregel en de beheerdersstartpagina

### Eerst het onderscheid dat de regel mogelijk maakt

De drie bestemmingen zijn niet drie soorten meldingen. Ze zijn drie verschillende dingen, en
het loont om dat eerst vast te leggen:

| Bestemming | Wat het technisch is | Wie het opruimt |
|---|---|---|
| **Postvak** | een rij in `notification_deliveries` per persoon | de ontvanger, door te lezen |
| **Beheerdersoverzicht** | een **pagina die de brontoestand bevraagt**: goedkeuringen, ArgoCD, markeringen, taken. Geen rij. | niemand; hij is altijd actueel |
| **Opskanaal** | een bericht naar ntfy (`opi/services/log_watcher.py`) | de dienstdoende |

**Het overzicht leest de brontoestand en niet de meldingentabel.** Dat is de belangrijkste
ontwerpregel van dit document. Het gevolg is dat "naar het overzicht" hetzelfde betekent als
"er wordt geen rij aangemaakt". Er is dus geen dubbeling, geen tweede leesstatus, en geen
vraag wat er gebeurt als je iets op het overzicht wegklikt.

En het gevolg voor de kosten: een gebeurtenis die naar het overzicht gaat, hoeft niet
uitgewaaierd te worden. Bij 137 deployments en 47 projecten scheelt dat precies het volume dat
deel 1 paragraaf 5 uitrekent.

### De grensregel

Drie toetsen. Je loopt ze in volgorde af. De eerste toets die slaagt bepaalt de bestemming, met
één uitzondering die eronder staat.

> **Toets 1, de eigenaarstoets.** Noem de **persoon** en noem de **handeling**.
> Gaat deze gebeurtenis over iets waar deze persoon eigenaar van is, en is hij ofwel
> **aan zet** (er wacht een handeling op hem die hij in ZAD kan doen en die de gebeurtenis
> afsluit) ofwel **gepasseerd** (er is namens hem iets met zijn eigendom gedaan wat hij niet
> meer kan terugdraaien)?
> **Slaagt** alleen als je de persoon met een rol kunt aanwijzen én de handeling in één
> werkwoord met een knop erachter kunt noemen, of kunt aanwijzen wat er onomkeerbaar is.
> Uitkomst: **postvak**, bij die persoon.
>
> **Toets 2, de toestandstoets.** Is de gebeurtenis het **verschil** in een toestand die ook
> zonder de gebeurtenis af te lezen valt?
> **Slaagt** alleen als je de regel kunt aanwijzen op een pagina die na deze gebeurtenis
> anders is.
> Uitkomst: **een pagina**. Welke pagina hangt af van wie je bent: het beheerdersoverzicht
> voor een platformbeheerder, de projectdetailpagina voor een projectbeheerder.
>
> **Toets 3, de klokketoets.** Moet er binnen een venster gereageerd worden dat korter is dan
> "de volgende keer dat iemand kijkt", ook buiten kantooruren, en ligt de reactie **buiten
> ZAD**?
> **Slaagt** alleen als er een dienstrooster is en de handeling niet in ZAD zit.
> Uitkomst: **opskanaal**.

**De uitzondering op de volgorde.** Toets 3 is niet uitsluitend: hij loopt náást de andere
twee. Een storing die het hele platform raakt, gaat naar ntfy én staat op het overzicht. Dat
is geen dubbeling maar twee publieken: wie dienst heeft en wie beheerder is, zijn niet
dezelfde persoon.

**De sluitregel.** Een gebeurtenis die geen van de drie haalt, wordt wél vastgelegd in
`notification_events` en krijgt géén bestemming. Dat is een geldige uitkomst en geen gat: hij
staat in de geschiedenis, hij is opvraagbaar, en niemand wordt ervoor wakker gemaakt.

**Waarom deze regel toetsbaar is.** Elke toets vraagt om iets **aan te wijzen**: een persoon,
een handeling, een regel op een pagina, een rooster. Twee mensen die het oneens zijn, zijn het
oneens over een aanwijsbaar ding en niet over een gevoel. Dat is het verschil met "is dit
belangrijk genoeg".

**Waar de regel het lastigst is, en dat is met opzet.** Toets 1 is streng: hij eist een
handeling of een onomkeerbaarheid. Dat betekent dat "je zou dit moeten weten" **niet**
voldoende is voor een postvakrij. Dat is de bedoeling, want "je zou dit moeten weten" is de
zin waarmee elk vol postvak begint.

### De twaalf typen langs de regel

De typen komen uit `plans/meldingen-inventarisatie.md`, "De groepering naar type". Per type
staat de uitkomst per rol, want de regel geeft niet één antwoord per type: dat is de kern van
de correctie op RC-148.

| # | Type | Projectbeheerder / projectlid | Platformbeheerder | Toets |
|---|---|---|---|---|
| 1 | `uitrol` | mislukt: **postvak** bij de actor en de projectbeheerder (handeling: opnieuw uitrollen, of het image terugdraaien). Geslaagd: **pagina** (de deploymentkaart) | **overzicht** | 1 slaagt bij mislukking voor wie het project bezit; voor de platformbeheerder is er geen handeling, dus 2 |
| 2 | `verwijdering` | **postvak** (gepasseerd: het is weg) | **overzicht** | 1 op de tak "gepasseerd" |
| 3 | `gezondheid` | **postvak** bij crashlus, OOM en image-pull (handeling: resources of image) | **overzicht**, en bij een platformbrede storing óók **ntfy** | 1, dan 2, en 3 loopt ernaast |
| 4 | `platform-ingreep` | **postvak** (gepasseerd: het platform veranderde zijn deployment) | **overzicht** | 1 op de tak "gepasseerd"; dit is de tak waarvoor die tak bestaat |
| 5 | `gegevens` | backup mislukt: **postvak** (handeling: schema of planning nakijken). Backup geslaagd: **pagina** | **overzicht** | 1 alleen bij mislukking |
| 6 | `aanvraag-ingediend` | n.v.t. | **postvak** | 1, en dit is de schoonste doorgang van de hele lijst |
| 7 | `aanvraag-besloten` | **postvak** bij de aanvrager | **overzicht** | 1 bij de aanvrager (aan zet: hij kan nu verder, of hij moet iets anders bedenken) |
| 8 | `leden-en-toegang` | **postvak**, en bij een wijziging aan je eigen toegang niet uitzetbaar | **overzicht** | 1 op de tak "gepasseerd" |
| 9 | `dienstwijziging` | **pagina** (de projectdetailpagina toont de diensten) | **overzicht** | 2 |
| 10 | `werkomgeving` | **postvak** bij de andere projectbeheerders (een console op de productiedatabase is iets waar collega's van horen te weten), **pagina** bij de actor zelf | **overzicht** | 1 op de tak "gepasseerd", ruim uitgelegd; zie de wrijving hieronder |
| 11 | `platform-mededeling` | onderhoud: **postvak** voor iedereen (handeling: niet uitrollen tijdens het venster). Release: **pagina** | idem | 1 voor onderhoud, 2 voor een release |
| 12 | `beheer` | n.v.t. | **overzicht**, behalve "iemand is platformbeheerder geworden of afgevoerd" en "beheer overgenomen van project X": die twee naar het **postvak** van de andere platformbeheerders | 2, met twee uitzonderingen die op tak "gepasseerd" van toets 1 slagen |

### Hoe de regel in de gegevens landt

De toewijzing hierboven is niet iets dat per melding met de hand gedaan wordt. Hij komt neer op
twee regels in het uitwaaieren, en die staan uitgewerkt in
`plans/meldingen-plan-van-aanpak.md` onder "De standaarden per rol":

1. **De reden is de sterkste aanspraak**, in de volgorde `actor` > `approver` >
   `platform-owner` > `project-admin` > `project-member` > `platform-user` > `platform-admin`.
2. **Wat als `platform-admin` overblijft, krijgt geen postvakrij**, en wat `informational` is
   ook niet.

De kolom "Platformbeheerder" van de tabel hierboven is dus geen aparte configuratie: hij is wat
er overblijft als je die twee regels toepast. De twee uitzonderingen in type 12 en de
onderhoudsberichten in type 11 zijn precies de gevallen waarvoor `platform-owner` en
`platform-user` bestaan.

### Waar het schuurt, en dat is de eerlijke helft

**Type 10 is de zwakste toewijzing.** "Een collega opende een databaseconsole op productie" is
niet echt "namens jou iets onomkeerbaars gedaan met jouw eigendom". Hij is het bijna: het is
jouw database, en de handeling is niet terug te draaien. Maar wie de toets streng leest, komt
op toets 2 uit en dus op de projectpagina. **Ik kies hier voor het postvak en dat is een
oordeel bovenop de regel**, met deze reden: dit is het enige type in de hele catalogus dat
tegelijk in een audittrail thuishoort (`plans/meldingen-inventarisatie.md`, paragraaf 8, "Een
console starten is een beheerdersgebeurtenis vermomd als gebruikersgebeurtenis"), en een
audittrail-gebeurtenis die niemand ziet is geen audittrail. Wie dit anders wil, verandert één
regel in de tabel en niet de regel zelf.

**Type 5 wringt aan de projectkant.** Een mislukte backup: kan de projectbeheerder daar iets
aan doen? Soms wel (een schema dat niet meer bestaat, een planning die niet klopt), vaak niet
(de opslag is vol, en dat is van het platform). De toets slaagt dus voor de ene helft en niet
voor de andere, en dat is met de gegevens die de gebeurtenis draagt niet uit elkaar te halen.
**Uitkomst: postvak, want de helft waarin hij wel iets kan doen is de helft waarin het ertoe
doet, en de andere helft is dan hoogstens ruis.** Dit is de plek waar de regel het minst
scherp snijdt en dat moet gezegd.

**Type 3 bij een platformbrede storing haalt twee toetsen.** Dat is opgelost met de
uitzondering op de volgorde (toets 3 loopt ernaast). Zonder die uitzondering zou de regel
kiezen tussen "op het overzicht" en "naar ntfy", en dat is een valse keuze.

**En de belangrijkste uitkomst van de hele oefening:** loop de kolom "Platformbeheerder" van
boven naar beneden. **Van de twaalf typen levert er precies één een postvakrij op die hij
krijgt omdát hij platformbeheerder is** (type 6, plus de twee uitzonderingen in type 12). Alle
andere gaan naar het overzicht. Dat is de correctie op "platformbeheerder: alles, inclusief
type 12", en hij komt niet uit een voorkeur maar uit de regel.

Die kwalificatie "omdát hij platformbeheerder is" hoort er letterlijk bij, want rij 11 zegt in
die kolom "idem" en dat is voor onderhoud óók een postvakrij. Die krijgt hij alleen niet als
beheerder maar als **gebruiker** van het platform, met `reason = platform-user`, precies zoals
iedere andere gebruiker die tijdens het onderhoudsvenster niet moet uitrollen. Wie de kolom
letterlijk natelt komt dus op twee, en de tweede staat niet op zijn beheerdersstapel.

### De beheerdersstartpagina

**Voorstel: `/beheer`.** Vijf blokken, in deze volgorde, en de volgorde is de boodschap: eerst
wat op jou wacht, dan wat kapot is, dan wat er buiten jou om gebeurd is, dan wat eraan komt,
en pas onderaan de toestand van de machinerie.

```
 ZAD  /  Beheer                                        [ 3 open aanvragen ]

 +------------------------------------------------------------------+
 | WACHT OP JOU                                                   3  |
 +------------------------------------------------------------------+
 | Domein  algor.rijksoverheid.nl      algor-odc      11 dagen  [>]  |
 | E-mail versturen                    wies-k2n        4 dagen  [>]  |
 | Subdomein  pr-450                   asses-k2n         2 uur  [>]  |
 |                                             alle aanvragen ->     |
 +------------------------------------------------------------------+

 +------------------------------------------------------------------+
 | NIET GEZOND                                                    4  |
 +------------------------------------------------------------------+
 | regel-k4c / pr-250     Degraded    CrashLoopBackOff       [>]     |
 | pm-5sj / acc           OutOfSync   sinds 3 dagen          [>]     |
 | wies / test-7          Missing     geen ArgoCD-app        [>]     |
 | asses-k2n / pr-450     Progressing al 40 minuten          [>]     |
 +------------------------------------------------------------------+

 +------------------------------------------------------------------+
 | HET PLATFORM HEEFT ZELF                            afgelopen 24u  |
 +------------------------------------------------------------------+
 | 01:00  geheugen bijgesteld op 11 componenten in 7 projecten  [>]  |
 | 01:00  plafond bereikt bij regel-k4c/api (4096Mi)            [>]  |
 | 03:00  reconciliatie: 0 opgeruimd, 2 gemarkeerd (proefstand) [>]  |
 +------------------------------------------------------------------+

 +------------------------------------------------------------------+
 | KOMT ERAAN                                                        |
 +------------------------------------------------------------------+
 | 2 markeringen verlopen binnen 7 dagen                       [>]   |
 | 5 deployments zonder backup in de laatste 48 uur            [>]   |
 | Certificaatverval: niet gemeten (bestaat niet, zie deel 1)        |
 +------------------------------------------------------------------+

 +------------------------------------------------------------------+
 | GEDEELDE DIENSTEN                                                 |
 +------------------------------------------------------------------+
 | PostgreSQL  ok   Keycloak  ok   Opslag  waarschuwing (81%)        |
 | Redis en MinIO worden niet gemeten                                |
 |                                          dienstenstatus ->        |
 +------------------------------------------------------------------+
```

**Wat elk blok bevraagt, en waarom het goedkoop is:**

| Blok | Bron | Kosten |
|---|---|---|
| Wacht op jou | `_collect_all_projects_approval_data` (`opi/web/router_approvals.py:161`), plus de openstaande weesbevestigingen | de bestaande functie, gesorteerd op ouderdom in plaats van op projectnaam |
| Niet gezond | ArgoCD | **nul extra bevragingen**: `get_project_argocd_statuses` haalt vandaag alle applicaties op en gooit die van andere projecten weg (`opi/services/argocd_overview.py:13-15`). Er is een variant nodig die niet weggooit, geen nieuwe verbinding. |
| Het platform heeft zelf | de `resources.history`-blokken in de projectbestanden, plus de uitkomst van de nachtelijke reconciliatie | de projectbestanden staan al in de `ProjectStore`; de reconciliatie-uitkomst moet ergens landen, want die gaat vandaag alleen naar de logregel `opi/jobs/reconciliation.py:400` |
| Komt eraan | `MarkedForDeletionService.get_all_marks` (`opi/services/marked_for_deletion_service.py:163`), en de laatste backup per deployment | de eerste bestaat en heeft vandaag geen enkele lezer met een scherm; de tweede vraagt een bevraging bij Kopia |
| Gedeelde diensten | `haal_opslag`, `haal_databases`, `haal_keycloak` uit `opi/services/gedeelde_diensten.py` | bestaat, wordt hergebruikt als samenvatting met een link door |

**Drie van de vijf blokken zijn dus opgebouwd uit gegevens die vandaag al opgehaald worden en
weggegooid of alleen gelogd.** Dat is de reden dat dit een goedkope pagina is en geen project.

**Wat er nadrukkelijk NIET op staat:**

- **Geen meldingen.** Het overzicht leest de brontoestand, niet `notification_deliveries`. Wie
  hier een postvak neerzet, bouwt hetzelfde probleem opnieuw op een andere pagina.
- **Geen grafieken.** Dat is Grafana en de metrics-explorer.
- **Geen lijst van alle projecten.** Dat is `/projects`, en die bestaat.
- **Geen kosten.** Dat is een maandvraag, geen dagvraag; `/admin/usage` blijft waar hij is.
- **Geen logs.** Dat is de logbewaker en ntfy.
- **Geen per-projectdetail.** Elke regel is een link naar de plek die het detail al toont.
- **Geen automatische verversing sneller dan een minuut.** Zie de verversingsparagraaf in
  `plans/meldingen-plan-van-aanpak.md`.

**Over de vorm.** De bouwlijn is `features/lotc-bouwlijn.md`. (De opdracht noemt
`ROOS_CLAUDE_REFERENCE.md` in `jinja-roos-components`; dat pad bestaat hier niet en dat klopt,
want die bibliotheek is sinds RC-67 uit het project verdwenen, zie `features/roos-eruit.md` en
de opmerking daarover in `plans/meldingen-plan-van-aanpak.md`, Kanaal 1.) Wat er van de
componenten bruikbaar is, geteld in `opi/templates_lotc/`:

| Component | Vandaag in gebruik | Bruikbaar voor |
|---|---|---|
| `c-card` | 153 keer | het kader per blok |
| `c-alert` | 109 keer | het "niet gezond"-blok |
| `c-tag` | 44 keer | de statuswoorden |
| `c-table` | 26 keer | de rijen, als een blok kolommen nodig heeft |
| `c-metric` | 15 keer | de tellers rechtsboven per blok |
| `c-section-head` | 9 keer | de kop per blok |
| `c-detail-list` | 9 keer | een blok zonder kolommen |
| `c-badge` | **0 keer** | de teller in de kop; bestaat in de catalogus, maar is voor dit project nieuw |
| `c-activity` / `c-activity-item` | **0 keer** | het blok "het platform heeft zelf": een activity-item draagt precies actor, actie, onderwerp en tijdstip |
| `c-status-bar` | **0 keer** | het blok gedeelde diensten |

**De laatste drie zijn nul keer gebruikt.** Ze staan wel in de bibliotheek (84 componenten in
`registry.json` van `lord_of_the_components`), maar reken op een rondje vormcontrole in de
proefopstelling (`/lotc/bg/<pagina>`, `opi/web/lotc_fixtures.py`) en niet op kopieerwerk van
een bestaande pagina. En: de proefopstelling is publiek en zit in de release-image, dus
uitsluitend zichtbaar verzonnen waarden, geen echte projectnamen.

### Hoe deze pagina zich verhoudt tot de vijf die er al zijn

De opdracht zegt terecht dat een zesde pagina bij een beheerdeel dat al niet lekker is, een
antwoord is dat je moet verdedigen. Hier is de verdediging.

**Wat er niet gebeurt: samenvoegen.** De vijf bestaande pagina's beantwoorden elk één vraag,
en ze doen dat goed genoeg. Ze in elkaar schuiven levert één pagina op die vijf dingen doet, en
dat is de slechtste van alle uitkomsten.

**Wat er wel gebeurt: er komt een ingang boven de vijf.** De menugroep "Beheer"
(`opi/web/navigation_lotc.py:126`) krijgt `/beheer` als eerste item, en de vijf bestaande
blijven eronder staan, in deze volgorde:

```
Beheer
  Overzicht            /beheer            <- nieuw
  Aanvragen            /admin/approvals
  Dienstenstatus       /admin/diensten
  Gebruikersbeheer     /admin/users
  Gebruik & kosten     /admin/usage
  Metrics              /metrics-explorer
```

Twee wijzigingen aan die groep, allebei klein en allebei te verdedigen:

- **`/metrics-explorer` gaat naar onderen.** Het is geen ZAD-pagina maar een deurtje naar de
  Prometheus-UI met zes hard ingebouwde diensten
  (`opi/web/metrics_explorer_router.py:27`). Dat is naslag, geen dagelijks werk, en hij stond
  bovenaan omdat hij als eerste werd toegevoegd (`opi/web/menu.py:64`).
- **"Services status" heet "Dienstenstatus".** De rest van het menu is Nederlands en dit item
  niet; dat is een naam, geen verhuizing.

**Wat de startpagina van de vijf afneemt.** Niets aan functie, maar wel aan bezoek: wie vandaag
`/admin/approvals` en `/admin/diensten` elke ochtend opent om te kijken of er iets is, hoeft
dat niet meer. Dat is de winst en het is de hele bedoeling.

**Wat de startpagina toevoegt dat nergens anders past.** De blokken "niet gezond", "het
platform heeft zelf" en "komt eraan" beantwoorden drie van de acht vragen uit deel 1 paragraaf
4 die vandaag nul schermen hebben. Ze passen op geen van de vijf bestaande pagina's: ze gaan
over alle projecten tegelijk, en de vijf bestaande gaan over gebruikers, kosten, aanvragen,
gedeelde infrastructuur en Prometheus.

**De eerlijke tegenwerping.** Een zesde pagina is een zesde plek om te kijken zolang niemand
je vertelt dat er iets op staat. Daarom hoort de teller in de kop uit RC-148 hier ook echt bij,
en daarom is de volgorde van de fasering hieronder wat hij is.

---

## Deel 6: de fasering en de beslissingen

### Wat er aan fase 1 van RC-148 verandert

RC-148 zet fase 1 op "het datamodel, de outbox met zijn planner, het postvak, de teller, de
API voor lezen en markeren, en één bron: goedkeuringen"
(`plans/meldingen-plan-van-aanpak.md`, "Fase 1"). **Die keuze blijft staan en de grensregel
bevestigt hem**: goedkeuringen zijn de schoonste doorgang van toets 1 in de hele catalogus
(deel 3, type 6).

Er verandert wel iets, en het zijn drie dingen.

**1. Er komt een fase 0 voor: het overzicht, klein.**

De blokken "wacht op jou" en "niet gezond" van de startpagina vragen **geen tabel, geen
migratie, geen planner en geen nieuw datamodel**. Ze bevragen toestand die er al is, met een
functie die er al is (`_collect_all_projects_approval_data`) en een bevraging die vandaag al
gedaan wordt en weggegooid (`opi/services/argocd_overview.py:13-15`).

Waarom die volgorde:

- **De standaardentabel is het ding dat fout was.** Bouw je fase 1 eerst, dan leg je die tabel
  in code vast en erft fase 2 hem. Bouw je het overzicht eerst, dan is de grensregel concreet
  voordat hij wordt vastgelegd, want er is een pagina om "naar het overzicht" naartoe te laten
  wijzen.
- **Het haalt het grootste deel van het volume weg vóór het bestaat.** Volgens de tabel in deel
  3 levert van de twaalf typen er één een postvakrij op voor een platformbeheerder. Zonder een
  overzicht is er geen plek voor de andere elf, en dan komen ze alsnog in het postvak terecht,
  omdat "nergens" geen bestemming is die iemand durft te kiezen.
- **Het is klein.** Twee blokken op één pagina, geen nieuwe opslag.

**Wat het kost: de meldingen schuiven op met de bouwtijd van fase 0.** Dat moet gezegd, want de
opdrachtgever wacht op meldingen en niet op een pagina. Mijn oordeel is dat het het waard is,
omdat fase 1 zonder overzicht een standaard vastlegt die daarna weer gecorrigeerd moet worden,
en een correctie op een uitgerolde standaard is duurder dan wachten. Maar het is een oordeel
en het staat als beslissing 3 hieronder.

**2. Fase 1 levert de gecorrigeerde standaardentabel, niet de oude.**

Zie `plans/meldingen-plan-van-aanpak.md`, "De standaarden per rol", zoals bijgewerkt door deze
opdracht. Concreet: wat `reason = "platform-admin"` draagt, komt niet in een postvak.

**3. Fase 1 repareert de ontbrekende aanvraagdatum.**

De generieke dienstgebruik-goedkeuring schrijft bij het aanvragen een lege history
(`opi/services/catalog/approval.py:303`), waar domein en subdomein er wel een tijdstip in
zetten (`opi/connectors/subdomain.py:511` en `:552`). Gevolg: bij een `send-email`-aanvraag is
niet vast te stellen hoe lang hij ligt. Dat is één regel, en zonder die regel kan het blok
"wacht op jou" niet op ouderdom sorteren en kan de escalatievraag (deel 4 van de
meldingenopdracht) later niet beantwoord worden.

**Wat er NIET verandert aan fase 1**: de bestandenlijst, het datamodel, de outboxplanner, de
keuze voor een eigen planner in de lifespan, en de bewaartermijnen. Die staan en dit document
raakt ze niet.

### De volgorde in het geheel

| Fase | Wat | Nieuwe opslag |
|---|---|---|
| **0 (nieuw)** | `/beheer` met de blokken "wacht op jou" en "niet gezond"; de aanvraagdatum repareren; sorteren op ouderdom | geen |
| **1** | RC-148 fase 1 ongewijzigd, met de gecorrigeerde standaardentabel | de vier meldingstabellen |
| **1b (nieuw)** | de blokken "het platform heeft zelf", "komt eraan" en "gedeelde diensten" op `/beheer` | de reconciliatie-uitkomst moet ergens landen |
| **2** | RC-148 fase 2 (de bronnen erbij) | het dedupvenster |
| **2b (nieuw)** | de platformbeheerderskolom op `/admin/users`, en de knop "beheer overnemen" | een kolom plus een kleine tabel |
| **3, 4** | RC-148 fase 3 en 4 ongewijzigd | |

**Waarom 2b daar staat en niet eerder.** De knop "beheer overnemen" moet een gebeurtenis
aanmaken die bij de projectbeheerders landt. Voor fase 1 bestaat die machinerie niet, en een
knop die stil rechten uitdeelt is slechter dan geen knop.

### De openstaande beslissingen

Elk punt is met ja of nee te beantwoorden. De aanbeveling staat erbij.

**1. Eén platformbeheerdersrol houden, en geen leesrol of taakgerichte scheiding invoeren.**
*Aanbeveling: ja.* Met twee dragers op productie lossen model B en C geen bestaand probleem op
en kosten ze blijvend werk bij elke nieuwe route. Herzien zodra er een tweede soort mens is die
het overzicht moet zien.

**2. Een platformbeheerder krijgt in een project waar hij geen lid van is leestoegang en niet
de rol `admin`.**
*Aanbeveling: ja.* Het is één functie (`get_user_role_for_project`), en 24 gemeten grendels in
sjablonen, routes en API volgen automatisch omdat ze allemaal positieve lijsten zijn. Het sluit
meteen de weg waarlangs de projectsleutel van elke projectpagina te kopiëren is.

**3. De rolwaarde wordt `platform-viewer` (voorstel) en niet `member`.**
*Aanbeveling: ja.* `member` werkt ook, maar dan is meekijken op het scherm niet te
onderscheiden van meedoen, en dat is precies de verwarring die we weghalen.

**4. Er komt een knop "Beheer overnemen" met een verplichte reden, een melding aan de
projectbeheerders en een vervaltijd van 24 uur.**
*Aanbeveling: ja, en niet los van beslissing 2.* Zonder deze knop is de enige weg terug naar
schrijfrechten "zet jezelf in het projectbestand", en dat is stiller dan wat we wegnemen.

**5. Het beheerdersoverzicht leest de brontoestand en nooit de meldingentabel.**
*Aanbeveling: ja.* Dit is de regel die "naar het overzicht" gelijkstelt aan "geen rij", en
daarmee het volume uit deel 1 paragraaf 5 wegneemt in plaats van het te verplaatsen.

**6. De grensregel is de drie toetsen uit deel 3, met toets 3 ernaast in plaats van erna.**
*Aanbeveling: ja.* De twee plekken waar hij schuurt (type 10 en type 5) staan benoemd met de
gekozen uitkomst en de reden, zodat een ander oordeel één regel in een tabel is.

**7. `/beheer` wordt een zesde pagina en de eerste van de menugroep "Beheer"; de vijf
bestaande blijven ongewijzigd.**
*Aanbeveling: ja.* Samenvoegen levert één pagina op die vijf dingen doet. De drie nieuwe
blokken passen op geen van de vijf, want ze gaan over alle projecten tegelijk.

**8. Fase 0 (het overzicht, klein) gaat vóór fase 1 van RC-148, ook al vertraagt dat de
meldingen.**
*Aanbeveling: ja, maar dit is het punt waarop ik me het makkelijkst laat overtuigen.* Als de
opdrachtgever de meldingen eerder wil, is het alternatief werkbaar: bouw fase 1 met de
gecorrigeerde standaardentabel en met "overzicht" als een bestemming die nog geen pagina heeft.
Wat dan niet mag gebeuren is fase 1 bouwen met de oude standaardentabel.

**9. De platformbeheerderslijst verhuist van de configuratie naar de database, met de ConfigMap
als noodingang.**
*Aanbeveling: ja.* Dit is wat BIO 8.02.01 (kwartaalbeoordeling van speciale bevoegdheden,
`features/bio-network-access-no-vpn-compliance.md:146`) in één scherm vervulbaar maakt, en het
laat de regel "iemand is platformbeheerder geworden" uit de **bestaat nog niet**-kolom van de
eventcatalogus verdwijnen.

**10. `ADMIN_API_KEY` krijgt een besluit: of de zeven endpoints gaan achter
`require_platform_admin` en de sleutel vervalt, of de sleutel wordt op productie gezet en zijn
houders worden opgeschreven.**
*Aanbeveling: de eerste.* Een gedeeld geheim zonder identiteit levert gebeurtenissen op zonder
`actor`, en dat botst met alles wat dit document en RC-148 over herleidbaarheid zeggen. De
huidige toestand is de slechtste van de drie: gedocumenteerd als werkwijze
(`features/service-orphan-reconciliation.md:65`), en op productie 501.

**11. De aanvraagdatum wordt ook bij de generieke dienstgebruik-goedkeuring geschreven.**
*Aanbeveling: ja.* Eén regel in `opi/services/catalog/approval.py:303`, en zonder die regel is
er geen ouderdom om op te sorteren of te escaleren.

### De niet-doen-lijst

| Niet | Waarom |
|---|---|
| Een leesrol of een taakgerichte scheiding bouwen | twee dragers; geen kandidaten; blijvende kosten per route. Zie model B en C |
| `spec.approver` gaan uitlezen | het veld heeft drie waarden waarvan er één gebruikt wordt; uitlezen heeft pas zin als er een goedkeuring komt die bij een projectbeheerder hoort |
| Leestoegang van een platformbeheerder tot projecten afsluiten | levert geen beveiliging op (hij heeft clustertoegang), alleen omweg |
| De logstroom per project achter de rol zetten | logs lezen is de kern van eerstelijnswerk; als dit anders moet is het een eigen beslissing |
| Bestaande projectsleutels intrekken | dit voorstel gaat over de weg waarlangs een nieuwe sleutel te lezen is, niet over sleutels die al uit zijn |
| De vijf bestaande beheerpagina's samenvoegen | vijf goede antwoorden op vijf vragen worden dan één pagina die vijf dingen doet |
| Meldingen op het beheerdersoverzicht tonen | het overzicht leest de brontoestand; een postvak op het overzicht is hetzelfde probleem op een andere pagina |
| Een tweede meldingssysteem voor beheerders | er is er nog geen één; RC-148 is het systeem en dit document is de verdeling erin |
| Kosten en grafieken op de startpagina | maandvraag versus dagvraag; Grafana en `/admin/usage` doen dat al |
| Certificaatverval, image-veroudering en scanbevindingen nu bouwen | die gebeurtenissen bestaan niet in OPI; RC-148 houdt ze open via het inkomende endpoint in fase 4 |
| Keycloak-rollen op `is_platform_admin` afbeelden | Keycloak levert hier authenticatie en geen autorisatie; die verbouwing is een eigen opdracht en staat los van dit gat |
