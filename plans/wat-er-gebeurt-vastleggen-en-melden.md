# Wat er op het platform gebeurt: vastleggen en melden

Opdracht: lever een plan van aanpak op voor gebeurtenissen in ZAD. Drie vragen, in deze volgorde: **welke gebeurtenissen kennen we**, **hoe leggen we die vast**, en **hoe melden we ze**. Het resultaat is documentatie. Er wordt in deze taak geen productiecode geschreven, geen tabel aangemaakt en geen migratie toegevoegd.

## Waarom dit nodig is

Vandaag verdwijnt vrijwel alles wat er gebeurt. Een deployment die om drie uur 's nachts vanzelf meer geheugen kreeg, een backup die niet liep, een lid dat aan een project werd toegevoegd, een projectbestand dat op een schemafout strandde: het staat in een logregel die na ongeveer drie uur uit `kubectl logs` is verdwenen, of het staat nergens. De log watcher is het bewijs van hoe scheef dat staat. Om te weten of OPI gezond is grepen we onze eigen logregels uit Loki, filteren we die tegen een ignore-lijst van bekende ruis, en duwen we de rest naar ntfy. Dat is triage op proza, omdat er geen gebeurtenissen zijn om op te triageren.

Tegelijk is er meer aanwezig dan het lijkt. Er zijn twee tabellen die al precies dit werk doen voor hun eigen domein, er is een dienstensysteem met een eventregistry, er is OpenTelemetry ingebouwd maar uitgezet, en er is sinds kort een werkende eigen mailrelay. Het plan moet daarop voortbouwen en niet naast bestaande administratie een derde beginnen.

## Wat er nu is, gemeten op 22 augustus 2026

Deze inventaris is het startpunt, niet het antwoord. **Verifieer elk punt in de code voordat je het overneemt, en vul aan wat hier ontbreekt.** De opdracht is expliciet om deze lijst compleet te maken, niet om hem over te schrijven.

### Wat al persistent is

| Wat | Waar | Wat het al draagt |
|---|---|---|
| Taken, 23 soorten (`TaskType`) met zes toestanden (`AsyncTaskStatus`) | tabel `async_tasks`, `opi/core/async_task_schema.py`, `opi/core/async_task_service.py` | `project_name`, `deployment_name`, `cluster`, `created_by`, `current_step`, `subtasks`, `logs`, `events`, `result`, `error_message`, `attempt_count`, tijdstippen voor created/started/completed |
| Runs, de tijdelijke databaseconsoles | tabel `runs`, `opi/core/runs_schema.py` | `kind`, `status`, `started_by`, `ended_by`, `expires_at`, `error_message`; de docstring noemt zichzelf letterlijk "de administratie/history record" en "de audit trail" |

Dit zijn de twee plekken waar ZAD vandaag al vastlegt wie wat wanneer deed. Alles hieronder doet dat niet.

### Achtergrondprocessen die alleen loggen

Elk van deze draait in de lifespan van `opi/server.py` en schrijft zijn uitkomst uitsluitend naar de logger:

- `BackupScheduler` en de retentie-sweep (`opi/core/backup_scheduler.py`, `backup_retention_sweep.py`)
- `ResourceTuningScheduler`, de VPA-tuner die geheugen- en CPU-grenzen bijstelt in het projectbestand (`opi/core/resource_tuning_scheduler.py`)
- `ReconciliationScheduler` (`opi/core/reconciliation_scheduler.py`)
- de OOM-watcher, die na een deploy kijkt of er OOM-kills waren en dan zelf het geheugen ophoogt en herverwerkt, tot drie pogingen (`opi/services/oom_watcher.py`)
- `SleepModeScheduler` (`opi/services/catalog/sleep_mode/scheduler.py`)
- `DbConsoleReaper` (`opi/core/db_console_reaper.py`)
- de CAA-reconciler en de no-mail-reconciler op onze eigen DNS-zones (`opi/core/caa_reconciler.py`, `no_mail_reconciler.py`)
- `git_monitor`, die een gewijzigd projectbestand oppikt en herverwerkt (`opi/core/git_monitor.py`); let op dat validatiefouten hier stil worden geslikt, dat heeft eerder wekenlang alle deploys van een project geblokkeerd zonder dat iemand het zag
- de reconcile-poll van de ProjectStore (`opi/services/project_store.py`)
- `LogwatcherScheduler`, die zelf de enige bestaande meldketen vormt (`opi/core/logwatcher_scheduler.py`, `opi/services/log_watcher.py`)

### Toestand die wel wordt bepaald maar niet als gebeurtenis bestaat

- gezondheid en afwijkingen van een deployment: `DeploymentStateFact`, `errors` en `deviations` uit `opi/services/deployment_diagnostics.py`, zie `features/deployment-state-and-health.md` en `features/status-afwijkingen.md`. Dit wordt per paginabezoek opnieuw berekend en nergens bewaard, dus "sinds wanneer is dit rood" is onbeantwoordbaar.
- ArgoCD sync- en healthovergangen, renderfouten, image-pull-backoff, probe-kills versus echte crashes.

### Beveiligings- en toegangsgebeurtenissen

- inloggen en uitloggen: Keycloak heeft hier eigen audit events voor, die staan aan in de realm-blueprints maar zijn niet gecommit en staan op productie uit.
- een gebruiker die niet op de allowlist staat wordt naar `/permission-denied` geleid (`opi/middleware/authorization.py`), dat is een logregel.
- een mislukte API-sleutel of bearer-token levert een `logger.warning` en verder niets (`opi/api/endpoint_util.py`, `opi/api/user_token_auth.py`).
- lid toevoegen of verwijderen, uitnodiging aangemaakt of geaccepteerd, project aangemaakt of verwijderd: allemaal een taak of een commit, geen gebeurtenis.
- **belangrijk**: elke commit in de drie git-repositories wordt geschreven onder één vaste identiteit (`GIT_COMMIT_AUTHOR_NAME` in `opi/connectors/git.py`). De git-historie vertelt dus wél wat er veranderde en niet wie het deed. Wie-deed-wat bestaat alleen in `async_tasks.created_by` en `runs.started_by`, en alleen voor wat via een taak of een run liep.

### Wat er aan meld- en exportinfrastructuur ligt

- **ntfy**, in gebruik door de log watcher, gericht op het platformteam.
- **De eigen mailrelay (Stalwart)**, sinds 21 augustus 2026 weer aan en op productie geverifieerd. De `send-email`-dienst is er voor projecten die zelf mail versturen; er is nog geen pad waarlangs het platform een gebruiker mailt.
- **OpenTelemetry**, volledig als dependency aanwezig met instrumentatie voor FastAPI, httpx, aiohttp, asyncpg en SQLAlchemy, plus `opi/core/tracing.py`. Staat uit: `OTEL_ENABLED: bool = False`.
- **Prometheus**, `opi/core/metrics.py` exporteert vandaag alleen procesinterne toestand van OPI zelf, geen domeingebeurtenissen.
- **Kubernetes events**, worden per taak opgehaald met `kubectl get events` en in de `events`-kolom van `async_tasks` gezet (`opi/connectors/kubectl.py`).

## De begripsbotsing die als eerste opgelost moet worden

Het woord *event* betekent in deze codebase vandaag drie verschillende dingen, en het plan moet daar een uitspraak over doen voordat er iets gebouwd wordt:

1. `ActionEvent` en `UIEvent` (`opi/services/services_enums.py`, `features/service-event-hooks.md`): in-procesuitbreidingspunten waarop een dienst inhaakt. Dit is een dispatchmechanisme, geen geschiedenis, en het is bewust zo ontworpen.
2. De `events`-kolom op `async_tasks`: gekopieerde Kubernetes-events bij één taak.
3. Wat de vraag hier bedoelt: er is iets gebeurd op het platform dat het waard is te bewaren en mogelijk te melden.

Kies een term voor het derde en gebruik die consequent. Als dat "event" wordt, benoem dan wat er met de eerste twee gebeurt. Voorstel om af te wegen, niet om over te nemen: de derde *gebeurtenis* noemen, zodat de eerste twee ongemoeid blijven.

## Op te leveren documenten

Drie bestanden. Feiten, analyse en aanbeveling gescheiden, zodat een lezer die het oneens is met de aanbeveling de inventaris nog kan gebruiken.

**1. `features/futures/gebeurtenissen-inventarisatie.md`**

De complete lijst van gebeurtenissen die ZAD kan voortbrengen. Per gebeurtenis minimaal: waar hij ontstaat (bestand en functie), wie hem veroorzaakt (mens, agent, scheduler, cluster), op welk niveau hij hangt (platform, project, deployment, component), wat er op dat moment bekend is aan context, waar hij vandaag heen gaat (taak, logregel, nergens), en of iemand hem zou willen weten. Groepeer naar wie hem wil hebben, niet naar waar hij in de code staat: de gebruiker van een project, de beheerder van het platform, en de agent of het script dat op de API zit hebben verschillende lijsten, en dat onderscheid draagt de rest van het plan.

Markeer expliciet de gebeurtenissen die vandaag verloren gaan en die het duurst zijn om te missen. Onderbouw dat met wat er eerder is misgegaan; de post-mortems in `docs/` en de losse punten in `TODO.md` zijn daar bruikbaar voor.

**2. `features/futures/gebeurtenissen-vastleggen-en-melden.md`**

De oplossingsrichtingen, elk met wat hij kost, wat hij oplevert, en waarom hij afvalt of blijft staan. Minimaal deze vier vorken, en voeg toe wat je zelf tegenkomt:

*Waar leggen we vast.* Een eigen `gebeurtenissen`-tabel in rig-db in de lijn van `async_tasks` en `runs`, met Alembic-migratie. Of alleen gestructureerd loggen naar Loki en de retentie daar laten. Of OTLP, met de deps die er al liggen en de exporter die al is ingericht. Of Kubernetes-events op de projectnamespace. Of een combinatie waarin de database de waarheid is en OTLP de export. Weeg mee: bevraagbaar per project, overleeft een herstart, retentie, kosten, en of het antwoord op "sinds wanneer is dit rood" eruit komt.

*Welk recordformaat.* Een eigen minimaal schema, of CloudEvents 1.0 volgens het NL GOV profiel. Dat profiel is voor de Nederlandse overheid pas-toe-of-leg-uit en schrijft URN-notatie voor op `source` (`urn:nld:oin:<OIN>:systeem:<naam>`) en reverse-DNS op `type` met een `v`-suffix voor versies. Raadpleeg hiervoor de skill `standaarden:ls-notif`. De afweging is niet religieus: intern een eigen model met een CloudEvents-projectie op de rand kan een prima uitkomst zijn. Benoem wel wat het kost om het pas later te doen.

*Hoe melden.* Zet de kanalen naast elkaar en koppel ze aan een publiek: een tijdlijn in het portaal per project en per deployment (geen nieuwe infrastructuur, altijd beschikbaar, pull), mail via de eigen relay (nu pas mogelijk, vraagt abonnementen en een afmeldpad), ntfy (bestaat al, is voor het platformteam), een webhook per project (push, het model uit de Logius-standaard Abonneren, en het enige kanaal dat een agent of eigen tooling echt kan gebruiken), en Prometheus met Alertmanager voor het deel dat eigenlijk een metriek is. Doe een uitspraak over waar een abonnement wordt vastgelegd: in het projectbestand, waar de rest van de projectconfiguratie staat en waar een AGE-hercodering en een GitOps-diff aan vastzitten, of in de database, waar het geen commit veroorzaakt.

*Ruis en drempel.* Een meldsysteem zonder drempel wordt uitgezet, en dat is hier geen theorie: de log watcher heeft al een ignore-lijst, een dedup-venster van zes uur en een top-vijf-begrenzing nodig gehad om bruikbaar te blijven. Ontwerp dedup, severity en samenvoeging vanaf het begin mee, en beschrijf wat er gebeurt als OPI herstart terwijl de dedup-map in het geheugen staat.

Behandel daarnaast twee dwarsdoorsnijdende punten:

*Wie mag welke gebeurtenis zien.* Een gebeurtenis draagt projectscope, dus lezen loopt langs `is_user_authorized_for_project` (`opi/services/project_authorization.py`) en niet langs een eigen tweede regel. Platformgebeurtenissen zijn alleen voor beheerders. Let op wat er in een gebeurtenis terechtkomt: een `error_message` uit een connector kan een geheim dragen.

*Bewaartermijn en persoonsgegevens.* Een gebeurtenissenlog met e-mailadressen erin is een verwerking. Zeg wat de bewaartermijn is, waarom, en hoe er wordt opgeruimd. Raadpleeg de skill `bio` voor wat de BIO2 over logging en monitoring eist, en de skill `standaarden:ls-logboek` voor het Logboek Dataverwerkingen en de NEN 7513/OTLP-vorm. Als de conclusie is dat die standaarden hier niet gelden, schrijf dan op waarom; dat is ook een antwoord.

**3. `features/futures/gebeurtenissen-plan-van-aanpak.md`**

Het plan zelf. Eén aanbevolen richting, met de afgevallen richtingen in één zin per stuk zodat de keuze navolgbaar blijft. Daarna fasering, waarbij elke fase op zichzelf waarde heeft en apart uitgerold kan worden. Sluit af met de beslissingen die een mens moet nemen voordat er gebouwd wordt, elk met de opties en een aanbeveling. Neem in dit document ook een expliciete kleinste eerste stap op: als er maar één ding gebouwd wordt, wat dan, en waarom dat.

## Randvoorwaarden

- Nederlands, in de toon van de bestaande documenten in `features/`. Geen emoji. Alinea's op één regel, dus geen harde regelafbrekingen midden in een zin.
- Elk feit over de huidige situatie wijst een bestand aan. Wat je niet in de code hebt teruggevonden, staat er niet in, of staat er met de vermelding dat het niet geverifieerd is.
- Zelfbedachte namen voor tabellen, velden, eventtypes of endpoints worden gemarkeerd als voorstel. Ze mogen niet ongemerkt doorlopen tot iets wat op een besluit lijkt.
- Geen productiecode, geen migratie, geen wijziging aan bestaande modules. Wel mag `TODO_FUTURE.md` een verwijzing naar de drie documenten krijgen.
- Waar dit raakt aan werk dat al beschreven is, verwijs ernaar in plaats van het over te doen: `features/log-watcher.md`, `features/async-task-system.md`, `features/oom-kill-watcher.md`, `features/deployment-state-and-health.md`, `features/status-afwijkingen.md`, `features/service-event-hooks.md`, `features/futures/migrate-task-progress-to-database.md`, `features/service-orphan-reconciliation.md`.

## Wanneer dit af is

1. De drie documenten bestaan op de genoemde paden.
2. De inventaris noemt alle achtergrondprocessen uit de lijst hierboven, en is aantoonbaar aangevuld met wat daar niet in stond. Een reviewer die `opi/server.py` openslaat en de lifespan naloopt vindt geen scheduler die in de inventaris ontbreekt.
3. De begripsbotsing rond het woord *event* is expliciet beslecht, met een uitspraak over `ActionEvent`/`UIEvent`.
4. Elke vork uit document 2 heeft minstens twee uitgewerkte richtingen met kosten en baten, en een aanbeveling.
5. Document 3 noemt per fase een verifieerbare uitkomst en een kleinste eerste stap.
6. Geen enkel bestand buiten `features/futures/` en `TODO_FUTURE.md` is gewijzigd.
