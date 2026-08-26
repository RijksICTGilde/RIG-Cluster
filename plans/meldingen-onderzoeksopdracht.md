# Meldingen in ZAD: inventarisatie en plan van aanpak

**Dit is een onderzoeks- en schrijfopdracht, geen bouwopdracht.** De oplevering bestaat uit
markdown in `plans/`. Er wordt in deze PR geen productiecode gewijzigd, niets uitgerold en
geen migratie toegevoegd. Schemaschetsen mogen wel, maar dan als codeblok IN het document.

## Wat de wens is

ZAD heeft geen meldingen. Er gebeurt van alles (een deploy faalt, een aanvraag wacht op een
beheerder, een pod wordt door de OOM-killer geraakt, een backup mislukt, iemand wordt lid
gemaakt van een project) en niemand hoort er iets van tenzij hij toevallig op het juiste
scherm kijkt. Het uitgangspunt is het model van GitHub: meldingen zijn **persoonlijk**, er
zijn **veel soorten gebeurtenissen**, en een persoon stelt **per soort** in of en hoe hij
iets wil horen. Terug te zien in de UI en de API, en eventueel afgeleverd per e-mail of
Mattermost. Beheerdersgebeurtenissen en aanvragen vallen er nadrukkelijk ook onder.

Drie vragen moeten beantwoord worden, en het antwoord op alle drie moet in de oplevering
staan:

1. **Welke events hebben we?** Een uitputtende inventarisatie, uit de code, niet uit het hoofd.
2. **Hoe leggen we ze slim vast?** Eén datamodel dat per persoon en per type te bevragen is,
   inclusief leesstatus, dedup en bewaartermijn.
3. **Hoe melden we?** Per kanaal (UI, API, e-mail, Mattermost) wat het vraagt en wat er
   vandaag voor ontbreekt.

## Oplevering

Verplicht:

- `plans/meldingen-inventarisatie.md` -- de eventcatalogus (zie "Deel 1").
- `plans/meldingen-plan-van-aanpak.md` -- de aanbevolen weg, met fasering (zie "Deel 3").
- Per uitgewerkte oplossingsrichting een eigen document, of één vergelijkingsdocument met de
  richtingen naast elkaar op dezelfde beoordelingsassen. Minimaal drie richtingen (zie "Deel 2").

Stijl: Nederlands, in de vorm van de bestaande documenten in `plans/` (lees er twee voor je
begint, bijvoorbeeld `plans/mail-vervolgpunten.md` en `plans/bio2-compliance-analysis.md`).
Geen em-dashes. Elk punt op zichzelf leesbaar: wat het is, waar het zit, wat het voorstel is,
welke beslissing open staat. Verzin geen namen die als vaststaand overkomen: markeer een
zelfbedachte naam (van een tabel, een endpoint, een event) expliciet als voorstel.

## Deel 1: de inventarisatie

Loop de bronnen van gebeurtenissen langs en leg per event vast:

| Kolom | Betekenis |
|---|---|
| event | de gebeurtenis, in gewone taal |
| bron | het codeanker (`pad/bestand.py:regel` of module) waar hij vandaag ontstaat, of "bestaat nog niet" |
| onderwerp | waar hij over gaat: project / deployment / component / dienst / gebruiker / platform |
| belanghebbenden | wie er iets aan heeft, uitgedrukt in rollen, niet in personen |
| ernst | ter informatie / actie nodig / storing |
| standaardkanaal | wat een redelijke standaard is per rol, en waarom |

De bronnen die er in elk geval zijn, met de plek waar je moet kijken. Deze lijst is een
startpunt en géén afbakening: wat je verder tegenkomt hoort er ook in.

- **Asynchrone taken.** 23 taaksoorten met een levenscyclus (pending, claimed, running,
  completed, failed, cancelled): `opi/core/async_task_service.py` (`TaskType`,
  `AsyncTaskStatus`), `opi/api/task_models.py` (de resultaatmodellen per soort),
  `features/async-task-system.md`, `features/task-progress-view.md`, `features/task-steps.md`.
- **Aanvragen en goedkeuringen.** De generieke goedkeuringsweg met `ApprovalSpec`,
  `ApprovalStatus` (none/requested/approved/denied) en `ApproverScope` (platform-admin,
  project-admin, project-member): `opi/services/catalog/approval.py`,
  `opi/services/approvals.py`, `features/aanvragen-beheerpagina.md`. Vandaag: domeinen
  (publish-on-web) en `send-email` (`features/send-email.md`). Let op de vier momenten die
  elk een eigen melding verdienen: aanvraag ingediend, goedgekeurd, afgewezen, ingetrokken.
- **Gezondheid van deployments.** OOM-kill, ImagePullBackOff (die zet een component op
  `replicas: 0`, dus dat is een melding met gevolgen), CrashLoopBackOff, probe-kills:
  `opi/services/oom_watcher.py`, `opi/services/event_interpreter.py` (die classificeert al
  op `EventSeverity`: actionable / informational / noise, dat is bruikbaar materiaal),
  `opi/services/deployment_state.py`, `features/image-pull-backoff-detection.md`,
  `features/probe-kill-is-geen-crash.md`, `features/uitgeschakeld-is-niet-gezond.md`,
  `features/status-afwijkingen.md`, `features/argocd-render-error-surfacing.md`.
- **Automatisch ingrijpen door het platform.** De resource-tuner die geheugen of CPU
  bijstelt zonder dat iemand erom vroeg (`opi/services/resource_tuning_service.py`,
  `features/auto-resource-tuning.md`), slaapstand (`features/sleep-mode.md`), de
  weesopruiming (`features/service-orphan-reconciliation.md`,
  `opi/services/marked_for_deletion_service.py`). Dit zijn precies de gebeurtenissen waar de
  eigenaar achteraf van moet weten.
- **Backups en herstel.** `features/backup-system.md`, `features/scheduled-backups.md`,
  `features/backup-retention-sweep.md`. Een geplande backup die faalt is vandaag stil.
- **Leden, uitnodigingen en toegang.** `opi/manager/invite_manager.py`,
  `features/invite-system.md`, `features/invites.md`, `features/zad-external-user-support.md`,
  `opi/services/project_authorization.py` (rollen; `PROJECT_EDIT_ROLES` is admin en owner).
- **Beheerdersgebeurtenissen.** Gebruikersbeheer (`opi/services/user_admin_service.py`,
  `features/user-admin-crud.md`), de platform-adminlijst, clusterbrede zaken: een nieuwe
  release, onderhoud, bevindingen uit de beveiligingsscan
  (`features/security-scanning-pipeline.md`), `features/image-version-audit.md`.
- **Kortlopende workloads.** `opi/services/runs_service.py` (db-console, straks ad-hoc jobs):
  gestart, verlopen, opgeruimd.
- **Wat er al één kanaal heeft.** `opi/services/log_watcher.py` stuurt OPI-foutmeldingen naar
  **ntfy**. Dat is vandaag de enige werkende meldingsweg in het systeem. Beschrijf hoe die
  zich verhoudt tot het nieuwe: opgaan in, naast, of blijven wat het is (ops versus klant).

Sluit de inventarisatie af met een groepering naar **type**, want dat type is straks de
knop waar iemand per stuk aan draait. Te veel typen is een onbruikbaar instellingenscherm,
te weinig is een aan-uitknop die niemand gebruikt. Doe een voorstel voor het aantal en
onderbouw het.

## Deel 2: de oplossingsrichtingen

Werk minimaal drie richtingen uit, elk op dezelfde assen: wat het kost om te bouwen, wat het
kost om te draaien, hoe het schaalt, wat het de gebruiker oplevert, hoe het faalt, en wat
het blokkeert of juist openhoudt voor later. Eindig met één expliciete aanbeveling.

Als startpunt (de weging is aan jou, en een vierde eigen richting mag):

- **A. Doorgeefluik zonder postvak.** Geen eigen meldingenopslag: een gebeurtenis gaat direct
  naar een kanaal (mail, Mattermost, ntfy) en verder nergens heen. Goedkoop, en de UI-eis
  vervalt daarmee grotendeels. Benoem eerlijk wat je dan niet hebt (leesstatus, geschiedenis,
  bewijs, per-persoon-instellingen die iets voorstellen).
- **B. Eventlogboek met uitwaaieren bij lezen.** Eén onveranderlijke tabel met gebeurtenissen;
  wie wat ziet volgt uit een bevraging op abonnement en autorisatie op het moment van kijken.
  Weinig schrijfwerk, geen duplicatie. Kwestie: leesstatus per persoon, en de kosten van die
  bevraging bij groei.
- **C. Postvak per persoon (het GitHub-model).** Bij het ontstaan van een gebeurtenis wordt
  per belanghebbende een rij geschreven, met leesstatus, reden ("waarom zie ik dit") en
  draadvorming per onderwerp. Duurder in opslag en schrijfwerk, maar precies wat de wens
  beschrijft. Kwestie: wat te doen met iemand die na het feit lid wordt of geen lid meer is.
- **Overweeg als variant of als extra as:** de gebeurtenis vastleggen in het NL GOV
  CloudEvents-profiel voor notificatiediensten (roep de skill `standaarden:ls-notif` aan). Ook
  als we geen abonnementen naar buiten aanbieden kan het formaat van het eventrecord daarop
  aansluiten, en dat is later het verschil tussen een koppelvlak en een verbouwing. Weeg het,
  neem het niet blind over.

Behandel binnen de gekozen richting in elk geval:

1. **Waar de gebeurtenis ontstaat.** Los `emit()`-aanroepen door de code heen, of declaratief
   via het bestaande hakensysteem (`opi/services/catalog/events.py`, `ActionEvent`/`UIEvent`,
   `features/service-event-hooks.md`) zodat een dienst zijn eigen events meebrengt zoals hij
   nu al zijn eigen goedkeuringen meebrengt. Dit is de belangrijkste architectuurkeuze in het
   hele stuk: kies bewust en onderbouw.
2. **Betrouwbaar afleveren.** Een transactionele outbox in Postgres, wie hem leegdrinkt (de
   bestaande takenwerker of een eigen planner in de lifespan van `server.py`), opnieuw
   proberen, idempotentie, en wat er gebeurt als een kanaal plat ligt. Een melding mag niet
   verdwijnen omdat de mailrelay even weg was, en mag ook niet twintig keer aankomen.
3. **Dedup en samenvoegen.** Een deployment die twintig keer herstart is één melding, niet
   twintig. `log_watcher.py` heeft hier al een dedupmodel met een venster; kijk daarnaar.
4. **Verhouding tot het audittrail.** Zijn "wat is er gebeurd" (bewijs, onveranderlijk,
   compleet) en "wat moet jij weten" (persoonlijk, wegklikbaar) dezelfde tabel of twee? Neem
   de BIO mee (skill `bio`: logging en monitoring) en de eisen rond het logboek
   dataverwerkingen (skill `standaarden:ls-logboek`) voor zover die raken aan wat we opslaan
   over personen. Kort en concreet, geen compliance-opstel.
5. **Datamodel.** Concrete tabellen en kolommen, in de stijl die er al ligt: SQLAlchemy-model
   onder `opi/services/persistence/`, `Base` uit `opi/core/db.py`, migratie via Alembic
   (`opi/migrations/versions/`, autogenerate is gericht op de ORM-modellen). Benoem indexen
   en de bewaartermijn, en wie hem opruimt.

## Deel 3: de kanalen en het plan

Per kanaal: wat er nodig is, wat er vandaag al ligt, en wat het blokkeert.

- **UI.** Een postvak, een teller in de kop, en per gebeurtenis een weg naar het onderwerp.
  Wat is de verversingsweg: htmx-polling, of iets levends (er is al een
  websocket-router voor logs, `opi/api/logs_websocket_router.py`)? Houd je aan de
  componentenbouwlijn: raadpleeg
  `/Users/robbertuittenbroek/IdeaProjects/jinja-roos-components/ROOS_CLAUDE_REFERENCE.md`
  en `features/lotc-bouwlijn.md`. Een schets in tekst volstaat, geen implementatie.
- **API.** Endpoints in de vorm van `opi/api/v2` en de getypeerde modellen van
  `opi/api/task_models.py`. Denk aan ophalen, ongelezen-telling, als gelezen markeren,
  voorkeuren lezen en schrijven. En de vraag of er een uitgaande weg bij hoort (webhook of
  abonnement) of dat dat expliciet buiten scope valt.
- **E-mail.** Let op, dit is de valkuil van deze opdracht: **OPI verstuurt vandaag zelf geen
  mail.** `opi/connectors/mail.py` praat alleen met de beheer-API van de relay om accounts
  voor projecten aan te maken. Er is dus geen verzendweg voor het platform zelf. Zoek uit wat
  de goedkoopste eerlijke oplossing is (het platform als klant van zijn eigen dienst met een
  eigen account op de relay ligt voor de hand) en welke gevolgen dat heeft voor de afzender.
  Lees `features/send-email.md`: de relay dwingt `From:` af als
  `noreply-rijksapp+<project>@rijksoverheid.nl`, en dat is een identiteitsbeslissing, geen
  instelling. Behandel ook afmelden, dagelijkse samenvatting versus meteen, en hoeveel inhoud
  er in een mail thuishoort (mijn aanname, weerleg hem gerust: zo min mogelijk, met een link
  terug, want de mail verlaat ons vertrouwensgebied).
- **Mattermost.** Zoek uit welke Mattermost dit is en of wij hem kunnen bereiken vanaf het
  cluster (netwerkbeleid, uitgaand verkeer; ODCN is streng, zie
  `features/restrictive-network-policies.md` en de bekende beperkingen rond uitgaande
  verbindingen). Het echte vraagstuk is niet het versturen maar het koppelen: hoe weet ZAD
  welk Mattermost-account bij een persoon hoort, en wie legt die koppeling. Weeg een
  persoonlijk bericht via een bot tegen een inkomende webhook op een kanaal, en wees duidelijk
  dat een kanaalwebhook géén persoonlijke melding is.
- **Voorkeuren.** Het scherm waar iemand per type per kanaal een knop omzet, met werkbare
  standaarden per rol, en het antwoord op "waarom kreeg ik dit bericht". Beschrijf ook wat er
  niet uitgezet mag kunnen worden (als dat er is) en waarom.

Sluit af met een **fasering** waarin fase 1 in één PR-serie past en op zichzelf waarde heeft.
Noem per fase de bestanden die geraakt worden, en zet er een lijst bij van wat we bewust
**niet** doen. Eindig met de openstaande beslissingen voor de opdrachtgever, elk met jouw
aanbeveling erbij, zodat er ja of nee op te zeggen is.

## Randvoorwaarden

- Verifieer elke bewering over bestaande code tegen de code. Een anker dat je niet terugvindt
  hoort als "bestaat nog niet" in het document, niet als aanname.
- Verzin geen ontbrekende functionaliteit erbij. Als iets er niet is, is dat een bevinding.
- Raak geen productiecode aan, voer geen migratie op, rol niets uit, en verander niets in de
  sandbox of op productie.
- Houd het leesbaar voor iemand die de code niet kent, maar wel precies genoeg dat de bouwer
  er morgen mee aan de slag kan.

## Klaar als

1. `plans/meldingen-inventarisatie.md` bestaat, en elke regel in de eventtabel heeft een
   codeanker of staat expliciet als "bestaat nog niet". De acht bronnen uit Deel 1 komen alle
   acht terug.
2. Er staan minimaal drie oplossingsrichtingen uitgewerkt op dezelfde beoordelingsassen, met
   één expliciete aanbeveling en de reden waarom de andere afvallen.
3. Het aanbevolen datamodel staat er als concrete tabellen en kolommen, met indexen,
   bewaartermijn en de plek in `opi/services/persistence/` plus de migratieweg.
4. Elk van de vier kanalen heeft een eigen paragraaf met "wat is er nodig", "wat ligt er al"
   en "wat blokkeert". De constatering dat OPI zelf geen mail verstuurt staat er, met een
   voorstel.
5. Er staat een fasering waarin fase 1 in één PR-serie past, met per fase de te raken
   bestanden en een expliciete niet-doen-lijst.
6. De openstaande beslissingen staan als lijst aan het eind, elk met een aanbeveling.
7. De PR bevat alleen documentatie in `plans/`.
