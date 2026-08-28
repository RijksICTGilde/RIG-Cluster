# Gebeurtenissen in ZAD: plan van aanpak

## Status en meetbasis

Dit is deel 3 van drie. Deel 1 (`features/futures/gebeurtenissen-inventarisatie.md`) is het feitenmateriaal, deel 2 (`features/futures/gebeurtenissen-vastleggen-en-melden.md`) weegt de richtingen af, en dit document kiest er één en zet hem in fasen. Wie het oneens is met de keuze hieronder kan deel 1 en 2 los gebruiken.

Gemeten op commit `83ac4b9b` van 21 augustus 2026, niet op `main`. Zie deel 1 voor waarom dat verschil ertoe doet. Alle namen voor tabellen, kolommen, eventtypes en endpoints in dit document zijn **VOORSTEL**.

---

## De aanbeveling in één alinea

Leg gebeurtenissen vast in een eigen tabel in rig-db, als vijfde Alembic-migratie naast `async_tasks`, `runs`, `users` en `marked_for_deletion`, met een gesloten enum van soorten waarvan de waarden meteen in reverse-DNS-notatie staan zodat een CloudEvents-projectie later een hernoeming is en geen vertaling. Toon ze eerst in de portal als tijdlijn per project en per deployment, achter de autorisatie die er al is. Bouw pas daarna push-kanalen, en dan in de volgorde webhook, Alertmanager, mail, omdat dat de volgorde is van "in ons beheer" naar "afhankelijk van een keten die aantoonbaar nog niet af is".

## De afgevallen richtingen, één zin per stuk

- **Alleen gestructureerd loggen naar Loki:** de Loki-, Grafana- en Mimir-stack wordt geleverd en niet door ons beheerd, haar retentie is daarmee niet te verifiëren, en zij kent onze projectrollen niet, dus lezen zou langs een tweede autorisatieregel lopen.
- **OTLP als bron van waarheid:** `opi/core/tracing.py` heeft alleen een trace-exporter, er staat geen ontvanger in `infrastructure/`, en een tracingbackend is gemaakt voor bemonsterde spans en niet voor een jarenlang bewaard verslag.
- **Kubernetes-events op de projectnamespace:** de standaardretentie is een uur (de `--event-ttl`-standaard van de kube-apiserver), dus het probleem wordt verplaatst en niet opgelost, en de zichtbaarheid volgt namespacerechten in plaats van projectlidmaatschap.
- **CloudEvents als intern opslagformaat:** het profiel vraagt een OIN die nergens in deze repo staat en verbiedt persoonsgegevens in de context-attributen, terwijl onze actor een e-mailadres is; als projectie op de rand kan het wel.
- **Het derde begrip "event" noemen en `ActionEvent`/`UIEvent` hernoemen:** dat raakt de hele dienstencatalogus, de registry en de dispatch, en levert niets op behalve een woord.
- **Abonnementen in het projectbestand:** elke afmelding zou een commit, een AGE-hercodering en een herverwerking veroorzaken, en een afmeldpad met die drempel wordt niet gebruikt.
- **Mail als eerste meldkanaal:** naar buiten mailen werkt niet, bounces verdwijnen stil en de MTA-STS-lookup hangt (`TODO.md` punt 26, alledrie gemeten op 21 augustus 2026), dus dat zou een meldsysteem zijn dat stil faalt.
- **Vijf ernstniveaus:** niemand kan het verschil tussen twee middenniveaus uitleggen, waarna alles naar boven glijdt.
- **Dedupliceren bij het vastleggen:** dan is achteraf niet meer vast te stellen hoe vaak iets gebeurde, terwijl "vijftig keer in een uur" precies het signaal is dat je wilt.

---

## De kleinste eerste stap

**Als er maar één ding wordt gebouwd: de tabel plus precies één schrijfweg, namelijk de resource-tuner, en een tijdlijn op de deploymentpagina die hem toont.**

Waarom deze en geen andere.

De resource-tuner is de enige bron die vandaag al een compleet gevormde gebeurtenis produceert. `$defs/resource-history-entry` in `opi/schemas/project_v2.json` heeft al een `timestamp`, een `source` uit een gesloten lijst (`auto-tune`, `oom-watcher`, `manual`), een `deployment` en een `reason` in proza. Er ontbreekt alleen een actor, en bij een scheduler is de actor de scheduler. Er hoeft dus niets te worden bedacht over wat er in de gebeurtenis staat; het staat er al, alleen op de verkeerde plek.

Het is bovendien de gebeurtenis waar het startpunt van deze opdracht mee opent: "een deployment die om drie uur 's nachts vanzelf meer geheugen kreeg". Die is vandaag alleen te zien door het projectbestand open te slaan, en dat doet een gebruiker niet.

En het is de goedkoopste manier om te ontdekken of de kolomkeuze klopt, want deze ene soort raakt alle niveaus (project, deployment, component), heeft een niet-menselijke actor, en heeft zowel gestructureerde gegevens als een mensleesbare samenvatting.

**Verifieerbare uitkomst:** een nachtelijke tuningronde op de sandbox schrijft een rij in `gebeurtenissen`, en die rij is als regel zichtbaar op de deploymentpagina voor een projectlid en niet zichtbaar voor iemand die geen lid is. Een herstart van OPI daartussen verandert daar niets aan.

**Wat er expliciet níét in zit:** geen tweede bron, geen melding, geen abonnement, geen export, geen retentielus. Die komen in fase 2 en verder, en de tabel is zonder die dingen al bruikbaar.

---

## Fasering

Elke fase heeft op zichzelf waarde en is apart uit te rollen. Geen enkele fase is een voorwaarde voor de volgende in de zin dat hij anders niets doet; ze stapelen.

### Fase 1: de tabel, één bron, en een tijdlijn

*Wat.* De migratie (VOORSTEL: `opi/migrations/versions/005_add_gebeurtenissen.py`), een schrijfdienst in de lijn van `AsyncTaskService`, de gesloten soortenenum met één waarde erin, de schrijfweg in `opi/core/resource_tuning_scheduler.py`, en een tijdlijnblok op de deploymentpagina achter `is_user_authorized_for_project`.

*Waarde op zichzelf.* De vraag "waarom heeft mijn component ineens meer geheugen" is beantwoordbaar zonder het projectbestand te openen.

*Verifieerbare uitkomst.* Zie "de kleinste eerste stap" hierboven.

### Fase 2: de bronnen die het duurst zijn om te missen

*Wat.* Vier schrijfwegen erbij, gekozen uit de zeven duurste uit deel 1: het afkeuren van een projectbestand door de schemavalidatie (`opi/core/git_monitor.py:150`), de uitkomst van de nachtelijke reconciliatie inclusief wat er is opgeruimd (`opi/core/reconciliation_scheduler.py`), de uitkomst van een backup en van de retentiesweep (`opi/core/backup_scheduler.py`, `opi/core/backup_retention_sweep.py`), en het uitschakelen van een component na een image-pull-fout of een OOM-kill (`opi/services/oom_watcher.py`).

Plus een projecttijdlijn naast de deploymenttijdlijn, omdat drie van deze vier op projectniveau hangen.

*Waarde op zichzelf.* Dit is het punt waarop de tijdlijn de vraag "wat is er met mijn project gebeurd terwijl ik weg was" beantwoordt. Vanaf hier is er ook voor het eerst een verslag van wat de reconciliatie heeft weggegooid, wat vandaag na één logregel verdwijnt.

*Verifieerbare uitkomst.* Een projectbestand met een moedwillige schemafout wordt afgekeurd én levert een gebeurtenis op de projecttijdlijn; een reconciliatieronde met een gemarkeerde resource laat na afloop een regel achter die zegt wat er is opgeruimd, ook nadat de rij in `marked_for_deletion` weg is.

### Fase 3: wie deed het

*Wat.* De actor doorgeven op de menselijke en de agentwegen: de taakwegen dragen hem al (`created_by`, gezet in `opi/core/task_helpers.py:63`), de directe bewerkingswegen in `opi/web/router_detail_edit.py` niet. Plus de beveiligingsgebeurtenissen uit deel 1, groep B: een geweigerde allowlist-controle (`opi/middleware/authorization.py:117`), een geweigerde API-sleutel (`opi/api/endpoint_util.py`), een geweigerd bearer-token (`opi/api/user_token_auth.py:252`).

*Waarde op zichzelf.* Dit is de fase die de compenserende maatregel uit `features/bio-network-access-no-vpn-compliance.md` waarmaakt en die bevinding E uit `plans/technische-review-bio-en-nora-bevindingen.md` dicht: een verslag met een actor, dat langer bestaat dan een uur. Het is ook de fase die de openstaande regel uit de post-mortem-tijdlijn ("Controle toegang tot Wies/ZAD/Keycloak wijzigingen") in de toekomst beantwoordbaar maakt.

*Verifieerbare uitkomst.* Een lid toevoegen via de portal levert een gebeurtenis met het e-mailadres van de handelende beheerder; drie keer een verkeerde API-sleutel aanbieden levert drie beveiligingsgebeurtenissen op de platformtijdlijn en geen enkele op een projecttijdlijn.

*Let op bij deze fase.* Vanaf hier staan er persoonsgegevens in de tabel, en dus is de retentielus uit fase 4 geen luxe meer maar een voorwaarde. Ze kunnen ook samen worden uitgerold; dan is fase 3 en 4 één stap.

### Fase 4: retentie en redactie

*Wat.* Een opruimlus in de lijn van `cleanup_old_tasks` (`opi/core/async_task_service.py:670`), die na de gekozen termijn de actor pseudonimiseert in plaats van de rij te verwijderen, met een langere termijn voor beveiligingsgebeurtenissen. Plus één redactiefunctie op de schrijfweg, zodat een connectorfout nooit ongefilterd in `gegevens` belandt.

*Waarde op zichzelf.* De verwerking is begrensd en uitlegbaar, en de tabel groeit niet onbeperkt.

*Verifieerbare uitkomst.* Een gebeurtenis ouder dan de termijn heeft geen actor meer maar staat er verder nog steeds; een beveiligingsgebeurtenis van dezelfde leeftijd staat er nog wel volledig; een testgebeurtenis met een wachtwoord in het foutveld komt geredigeerd in de database terecht en niet pas geredigeerd op het scherm.

### Fase 5: het push-kanaal voor de agent

*Wat.* Een abonnementstabel (VOORSTEL: `gebeurtenis_abonnementen`), een bezorger met exponentiële herhaling en een bovengrens, een geheim per abonnement, en het watermerk per abonnement uit deel 2 zodat een herstart geen dubbele of gemiste bezorging oplevert. Uitgaand netwerkbeleid hoort hierbij: zodra een webhook een NetworkPolicy nodig heeft, hoort het aan-uit-vinkje in het projectbestand zoals bij `vlam` en `send-email`.

*Waarde op zichzelf.* De agent hoeft niet meer te pollen op een taakstatus die na een uur verdwijnt.

*Verifieerbare uitkomst.* Een testontvanger krijgt precies één aflevering voor een gebeurtenis waarop hij is geabonneerd; een ontvanger die 500 teruggeeft krijgt herhalingen met oplopende tussenpozen en geen oneindige lus; een OPI-herstart midden in een venster levert geen tweede aflevering van al bezorgde gebeurtenissen.

### Fase 6: de export, en de metriekhelft

*Wat.* Twee dingen die los van elkaar staan. De CloudEvents-projectie op de rand, met de `source` uit de OIN-beslissing hieronder. En Alertmanager plus alerteringsregels op Prometheus, voor wat eigenlijk een drempelwaarde over een reeks is (`infrastructure/bootstrap/infrastructure/prometheus/controller/base/configmap.yaml` heeft vandaag geen `rule_files` en geen `alerting`), met een teller per gebeurtenissoort in `opi/core/metrics.py`, dat vandaag alleen gauges kent.

*Waarde op zichzelf.* Een externe afnemer kan aansluiten zonder uitleg, en de dingen die een tijdlijn niet kan zeggen ("de wachtrij loopt op", "drie backups op rij gemist") krijgen het gereedschap dat daarvoor bedoeld is.

*Verifieerbare uitkomst.* Een uitgaande projectie valideert tegen het NL GOV profiel op de vier verplichte attributen; een alerteringsregel op een kunstmatig opgevoerde teller bereikt ntfy.

### Fase 7: mail, en pas als de keten af is

*Wat.* Een verzendweg vanuit OPI over het platformaccount dat er al is (`opi/manager/mail_manager.py:248`), sjablonen, en een afmeldpad dat geen commit veroorzaakt.

*Voorwaarde, niet context.* De vier punten uit `TODO.md` punt 26 zijn de toegangspoort: naar buiten mailen kan niet, bounces verdwijnen stil, de MTA-STS-lookup hangt, en het spamfilter staat uit. Zolang punt 2 open staat, is elke mail die niet aankomt onzichtbaar voor zowel de ontvanger als ons.

*Waarde op zichzelf.* Iemand die niet in de portal kijkt, hoort het toch.

*Verifieerbare uitkomst.* Een testabonnement levert precies één mail per venster met de gebeurtenissen gegroepeerd, de afmeldlink werkt zonder dat er een taak of een commit ontstaat, en een mislukte bezorging levert een zichtbare gebeurtenis op in plaats van stilte.

---

## Beslissingen die een mens moet nemen voordat er gebouwd wordt

### 1. Op welke tak dit werk landt

*Opties.* (a) Op de huidige ontwikkellijn (`release-augustus-2026` en verder), waar alles staat waar deze documenten naar verwijzen. (b) Op `main`, dat 1658 commits achterloopt en waar drie van de acht verwezen documenten en zes van de drieëntwintig achtergrondprocessen niet bestaan.

*Aanbeveling: (a).* Deze drie documenten zijn zuivere toevoegingen in `features/futures/` en mergen schoon op elke tak, maar hun inhoud is alleen waar op de ontwikkellijn. Dit is de eerste beslissing omdat elke andere ervan afhangt.

### 2. Wat "gebeurtenis" gaat heten in code, en of het besluit uit deel 2 blijft staan

*Opties.* (a) `Gebeurtenis`, Nederlands, met `ActionEvent`/`UIEvent` en de `events`-kolom ongemoeid. (b) `Event`, met een hernoeming van de bestaande twee families.

*Aanbeveling: (a).* Deel 2 werkt het argument uit: (b) raakt de hele dienstencatalogus en levert een woord op.

### 3. Waar een abonnement wordt vastgelegd

*Opties.* (a) In de database. (b) In het projectbestand. (c) Gemengd: het aan-uit-vinkje in het projectbestand als er een netwerkregel bij hoort, de rest in de database.

*Aanbeveling: (c), en (a) zolang er nog geen webhook is.* Een afmelding die een deployment veroorzaakt, is een afmelding die niet gebeurt; maar zodra een abonnement infrastructuur nodig heeft, hoort het aan-uit-vinkje te staan waar alle andere diensten staan.

### 4. De bewaartermijn, en of beveiligingsgebeurtenissen een eigen termijn krijgen

*Opties.* (a) 90 dagen voor alles, aansluitend bij `eventsExpiration: 7776000` in de Keycloak-configuraties. (b) 90 dagen voor gewone gebeurtenissen met pseudonimisering van de actor daarna, en een langere termijn voor beveiligingsgebeurtenissen. (c) Eén korte termijn voor alles, in de lijn van de huidige `TASK_WORKER_CLEANUP_RETENTION_HOURS: int = 1`.

*Aanbeveling: (b).* Voor gewone beheergebeurtenissen is 90 dagen ruim genoeg, maar BIO2 8.15.04 vraagt een risicogerichte termijn met langdurig aanwezige aanvallers in gedachten, en voor een beveiligingsspoor is 90 dagen dan waarschijnlijk te kort. (c) valt af: dat is de huidige toestand, die bevinding E in de technische review opleverde. **Hoe lang "langer" is, is de beslissing** en die is niet vanuit de code te nemen; het is een gesprek met wie verantwoordelijk is voor de risicoafweging in `features/bio-network-access-no-vpn-compliance.md`.

### 5. Of er een organisatie-OIN is, en welke

*Opties.* (a) Er is er een, en die wordt vastgelegd als instelling. (b) Er is er geen, en dan is een geldige CloudEvents `source` niet te bouwen en valt fase 6 gedeeltelijk weg.

*Aanbeveling: uitzoeken vóór fase 1, niet vóór fase 6.* De OIN zelf is pas nodig bij de export, maar het antwoord bepaalt of de soortnamen in fase 1 in reverse-DNS-notatie moeten staan. Zo niet, dan is die notatie onnodige omslachtigheid; zo wel, dan is hem later invoeren een migratie over de hele tabel.

### 6. Of de gezondheidsovergangen van een deployment gebeurtenissen worden

*Opties.* (a) Ja: bij elke berekening in `opi/services/deployment_diagnostics.py` wordt de uitkomst vergeleken met de vorige stand en levert een verandering een gebeurtenis op. (b) Nee: de berekening blijft een momentopname en alleen de dingen die OPI zelf doet worden vastgelegd.

*Aanbeveling: (a), maar niet vóór fase 4.* Dit is het enige dat "sinds wanneer is dit rood" echt beantwoordt, en het is tegelijk de grootste bron van ruis, want de berekening draait bij elk paginabezoek. Het vraagt een bewaarde vorige stand die er vandaag niet is, en het vraagt de drempels uit deel 2 vork 4 op hun plek. Het is de duurste van de zeven beslissingen en hoort daarom achteraan.

### 7. Wie de eerste lezer is

*Opties.* (a) De gebruiker van een project (tijdlijn eerst). (b) De beheerder van het platform (ntfy en Alertmanager eerst). (c) De agent (webhook eerst).

*Aanbeveling: (a).* Niet omdat de andere twee minder belangrijk zijn, maar omdat de tijdlijn de enige van de drie is die kan mislukken zonder dat iemand er last van heeft, en dus de enige waarop je kunt ontdekken dat je de verkeerde gebeurtenissen hebt gekozen. Het platformteam heeft bovendien al ntfy, en de agent heeft nog niets, maar de agent is ook de enige die kan wachten tot het formaat vaststaat.
