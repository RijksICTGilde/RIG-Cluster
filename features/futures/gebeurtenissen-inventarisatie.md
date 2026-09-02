# Gebeurtenissen in ZAD: de inventaris

## Status en meetbasis

Dit is deel 1 van drie. Het beschrijft alleen wat er is; de oplossingsrichtingen staan in `features/futures/gebeurtenissen-vastleggen-en-melden.md` en het plan in `features/futures/gebeurtenissen-plan-van-aanpak.md`. Er is voor deze inventaris geen code gewijzigd.

Alles hieronder is gemeten op commit `83ac4b9b` (21 augustus 2026), de tip van de ontwikkellijn waar `release-augustus-2026` deel van uitmaakt. Dat is bewust niet `main`: `main` staat op `51fd763e` van 27 juli 2026 en loopt 1658 commits achter, en drie van de documenten waar dit stuk naar verwijst (`features/deployment-state-and-health.md`, `features/status-afwijkingen.md`, `features/service-event-hooks.md`) bestaan daar nog niet, net zomin als zes van de drieëntwintig achtergrondprocessen uit de lijst verderop: de CAA-reconciler, de no-mail-reconciler, de reconciliatiescheduler, de slaapstandsweeper, de probeserver en het platform-mailaccount. Wie een regel hieronder wil natrekken en hem niet vindt, controleert eerst op welke tak hij staat: `git show 83ac4b9b:<pad>` werkt altijd.

Regels die niet in code te controleren waren staan gemarkeerd als **niet geverifieerd**. Zelfbedachte namen komen in dit document niet voor; die staan in deel 2 en zijn daar als voorstel gemarkeerd.

## Woordkeuze

Het woord *event* is in deze codebase al twee keer bezet. Dit document gebruikt daarom consequent **gebeurtenis** voor het derde begrip: iets dat op het platform is gebeurd en dat het waard is te bewaren en mogelijk te melden. De volledige afweging en wat dat voor `ActionEvent`, `UIEvent` en de `events`-kolom betekent staat in deel 2 onder "De begripsbotsing".

## Hoe deze lijst is geordend

De ordening is naar **wie de gebeurtenis wil hebben**, niet naar waar hij in de code staat. Dat onderscheid draagt de rest van het plan: de drie publieken hebben andere lijsten, andere drempels en andere kanalen.

- **De gebruiker van een project** wil weten wat er met zijn eigen project gebeurde, ook als hij er niet bij was. Zijn horizon is een project.
- **De beheerder van het platform** wil weten of het platform gezond is en wie wat deed. Zijn horizon is het cluster.
- **De agent of het script op de API** wil een machineleesbaar signaal om op te reageren, in plaats van te pollen. Zijn horizon is een project, maar via een sleutel in plaats van een sessie.

Per gebeurtenis staan zes dingen: waar hij ontstaat, wie hem veroorzaakt, op welk niveau hij hangt, wat er op dat moment bekend is, waar hij vandaag heen gaat, en of iemand hem zou willen weten.

De veroorzakers zijn: **mens** (een sessie in de portal), **agent** (een API-sleutel of bearer-token), **scheduler** (een achtergrondlus van OPI zelf), **cluster** (kubelet, ArgoCD, CNPG) en **buiten** (een directe push naar git, een upstream mailserver, een DNS-houder).

---

## Wat vandaag al persistent is

De taak zegt dat er twee tabellen zijn die dit werk al doen. Er zijn er vijf, plus drie geschiedenissen die in het projectbestand zelf staan. Dat is de belangrijkste correctie op het startpunt: naast bestaande administratie een derde beginnen is niet het risico; het risico is naast **acht** bestaande administraties een negende beginnen.

| Wat | Waar | Sleutelvelden | Wat het niet draagt |
|---|---|---|---|
| Taken, 23 soorten (`TaskType`) met zes toestanden (`AsyncTaskStatus`) | tabel `async_tasks`, `opi/core/async_task_schema.py`, `opi/core/async_task_service.py:54` | `project_name`, `deployment_name`, `cluster`, `created_by`, `current_step`, `progress_percent`, `subtasks`, `logs`, `events`, `web_addresses`, `result`, `error_message`, `attempt_count`, `created_at`/`started_at`/`completed_at` | Wordt standaard na **1 uur** verwijderd (`TASK_WORKER_CLEANUP_RETENTION_HOURS: int = 1`, `opi/core/config.py:369`, gebruikt in `opi/core/task_worker.py:426`) |
| Runs, de tijdelijke databaseconsoles en jobbundels | tabel `runs`, `opi/core/runs_schema.py` | `kind`, `status`, `started_by`, `ended_by`, `expires_at`, `error_message`, `namespace`, `spec` | Alleen deze ene soort werklast; de docstring noemt zichzelf letterlijk "the administration/history record" en "the audit trail" |
| Platformgebruikers | tabel `users`, `opi/core/user_schema.py` | `email`, `full_name`, `created_at`, `updated_at` | Wie de gebruiker aanmaakte of wijzigde; alleen de laatste toestand, geen historie |
| Gemarkeerd voor verwijdering | tabel `marked_for_deletion`, `opi/core/marked_for_deletion_schema.py` | `resource_type`, `resource_name`, `project_name`, `deployment_name`, `cluster`, `marked_at`, `metadata` | Wie de markering veroorzaakte; en de rij verdwijnt zodra de reconciliatie opruimt, dus er blijft niets over dat zegt dat er iets is opgeruimd |
| Subdomeinregister | tabel `subdomain_registry`, `opi/services/persistence/subdomain_registry.py:737` | `subdomain`, `base_domain`, `project_name`, `deployment_name`, `cluster`, `created_at`, `created_by` | Alleen de huidige claim; een vrijgegeven subdomein laat geen spoor na |
| Resource-historie per component | projectbestand, `$defs/resource-history-entry` in `opi/schemas/project_v2.json` | `timestamp`, `limits`, `requests`, `source` (`auto-tune`/`oom-watcher`/`manual`), `deployment`, `reason` | Geen actor. Dit is feitelijk al een gebeurtenissenlog voor één domein, met een `reason` in proza |
| Dienstrevisies | projectbestand, `$defs/service-revision` in `opi/schemas/project_v2.json` | `generation`, `resource`, `status`, `created_at`, `superseded_at`, `actions[].type`/`.source`/`.timestamp` | Geen actor; zie `features/service-revision-tracking.md` |
| Kloonstatus | projectbestand, `$defs/clone-from` in `opi/schemas/project_v2.json` | `status.completed`, `status.timestamp` | Eén boolean plus een tijdstip; geen uitkomst, geen actor |

Alle overige tabellen bestaan niet: er zijn vier Alembic-migraties (`opi/migrations/versions/001_baseline.py` tot en met `opi/migrations/versions/004_add_runs.py`) en die dekken precies de vijf tabellen hierboven.

### De blinde vlek die alles hieronder kleurt

Elke commit in `zad-projects`, `zad-argo-user-applications` en `zad-deployments` wordt geschreven onder één vaste identiteit: `GIT_COMMIT_AUTHOR_NAME = "Operations Manager"` en `GIT_COMMIT_AUTHOR_EMAIL = "operations-manager@example.com"` (`opi/connectors/git.py:53-54`, doorgegeven als `GIT_AUTHOR_NAME`/`GIT_COMMITTER_NAME` op regel 1831-1834). De commitboodschap beschrijft wat er veranderde, nooit wie het vroeg; zie bijvoorbeeld `opi/manager/project_manager.py:7434` (`Add deployment '<naam>' to project '<naam>'`) en `:7612`.

Wie-deed-wat bestaat dus alleen in `async_tasks.created_by` en `runs.started_by`. En `async_tasks` wordt na een uur opgeruimd. Een technische review van dit punt staat al in `plans/technische-review-bio-en-nora-bevindingen.md`, bevinding E, met dezelfde conclusie en met BIO2 8.15.01 erbij: die overheidsmaatregel schrijft een logregel voor met minimaal actie, object, resultaat, oorsprong, **actor** en tijdstempel, en precies de actor ontbreekt.

Twee dingen die die review noemt zijn ook hier van belang. Ten eerste dragen de logregels lokale Amsterdamse tijd zonder offset (`log_format` in `opi/utils/logging_config.py:48` gebruikt de kale `%(asctime)s`), wat tijdcorrelatie rond de zomertijdovergang foutgevoelig maakt. Ten tweede staat er al een correlatie-identificatie in elke logregel: `flow_id`, uit `opi/core/flow_id.py`, met een voorvoegsel per soort stroom (`req-`, `task-`). Die is er dus al en is niet gekoppeld aan iets dat bewaard blijft.

---

## Groep A: wat de gebruiker van een project wil weten

| Gebeurtenis | Ontstaat in | Veroorzaker | Niveau | Context op dat moment | Gaat vandaag heen naar | Willen weten |
|---|---|---|---|---|---|---|
| Mijn deployment kreeg vannacht vanzelf meer geheugen | `opi/core/resource_tuning_scheduler.py:120` en `opi/services/resource_tuning_service.py` | scheduler | component | project, component, oude en nieuwe grens, reden | logregel plus een regel in `resources.history` van het projectbestand (mét reden, zonder actor) | Ja, hoog |
| Een OOM-kill is gedetecteerd en het geheugen is opgehoogd, tot drie pogingen | `opi/services/oom_watcher.py:704` (`schedule_oom_check`) | cluster, daarna scheduler | component | project, deployment, component, poging | een `REFRESH_DEPLOYMENT`-taak plus `resources.history` met `source: oom-watcher` | Ja, hoog |
| Een component is uitgeschakeld wegens een image-pull-fout | `opi/services/oom_watcher.py` (image-pull-tak) en `opi/handlers/project_file_handler.py` | cluster, daarna scheduler | component | project, deployment, component, foutmelding van de kubelet | een taak die `replicas: 0` zet; zie `features/image-pull-backoff-detection.md` | Ja, hoog |
| Mijn backup is gelukt, of juist niet gelopen | `opi/core/backup_scheduler.py:268` en `:302` | scheduler | project | project, snapshot-tijdstip, Kopia-uitkomst | een `BACKUP`-taak (weg na een uur) en logregels | Ja, hoog |
| Er zijn oude snapshots opgeruimd door de retentiesweep | `opi/core/backup_retention_sweep.py:223` en `:226` | scheduler | project | project, namespace, aantal snapshots, wat weg mocht | uitsluitend logregels; zie `features/backup-retention-sweep.md` | Ja |
| Mijn projectbestand is afgekeurd door de schemavalidatie en is niet verwerkt | `opi/core/git_monitor.py:150` | mens of buiten (directe push) | project | bestandspad, de schemafout | een `logger.error`, verder niets | Ja, zeer hoog |
| Mijn deployment is in slaap gevallen of gewekt | `opi/services/catalog/sleep_mode/scheduler.py:208` en `:227`, `opi/services/catalog/sleep_mode/flow.py:98` | scheduler, of een bezoeker via de wekknop | deployment | project, deployment, deadline, aanleiding | `SLEEP_DEPLOYMENT`/`WAKE_DEPLOYMENT`-taak plus een toestandsveld in het projectbestand; zie `features/sleep-mode.md` | Ja |
| Er is een lid toegevoegd of verwijderd bij mijn project | `opi/web/router_detail_edit.py` via `save_and_commit_project` | mens | project | project, e-mailadres, rol | een commit onder de systeemidentiteit; welke mens het deed staat nergens | Ja, hoog |
| Er is een uitnodiging aangemaakt of geaccepteerd | `opi/api/invite_routes.py:604` (SSO-terugkomst) en `:721` (registratie) | mens | project | project, uitnodigingssleutel, e-mailadres uit het token | `logger.info`, en het account in Keycloak | Ja |
| Mijn project is aangemaakt of verwijderd | `TaskType.CREATE_PROJECT` / `DELETE_PROJECT`, `opi/core/task_handlers_project.py` | mens of agent | project | alles uit het projectbestand, plus `created_by` | een taak (weg na een uur) en commits | Ja, hoog |
| Er is een component of dienst toegevoegd, gewijzigd of verwijderd | `opi/core/task_handlers_components.py` | mens of agent | component | project, deployment, component, dienst | een taak plus commits | Ja |
| Mijn database, bucket of PVC is opgeruimd door de nachtelijke reconciliatie | `opi/core/reconciliation_scheduler.py:100`, uitvoering in `opi/jobs/reconciliation.py` | scheduler | project | project, resourcesoort, resourcenaam, gemarkeerd sinds | één logregel met tellingen (`purged=%d, unmarked=%d, errors=%d`); de rij in `marked_for_deletion` verdwijnt bij het opruimen | Ja, zeer hoog |
| Mijn deployment is rood, en sinds wanneer | `opi/services/deployment_diagnostics.py:102` (`gather_deployment_errors`) en `:262` (`gather_sync_deviations`) | cluster | deployment | ArgoCD-status, resource, boodschap, categorie | nergens: dit wordt per paginabezoek herberekend en niet bewaard; zie `features/deployment-state-and-health.md` en `features/status-afwijkingen.md` | Ja, hoog |
| Een mail die mijn project verstuurde is niet bezorgd | de relay (Stalwart), buiten OPI | buiten | project | ontvanger, foutcode van de upstream | niets: de DSN wordt aan een adres gericht dat de upstream ook weigert, waarna de relay "discarding message after double bounce" noteert en het bericht weggooit; de relaylog bewaart drie uur (`TODO.md` punt 26, uitgewerkt in `plans/mail-vervolgpunten.md`) | Ja, zeer hoog |
| Mijn database is gekloond, of een restore is uitgevoerd | `opi/core/task_handlers_operations.py`, `opi/core/task_handlers_backup.py` | mens of agent | deployment | bron, doel, uitkomst | een taak (weg na een uur) | Ja |
| Er is een tijdelijke databaseconsole geopend op mijn project | `opi/core/runs_schema.py`, gestart via `opi/manager/run_support.py:56` | mens | deployment | wie, wanneer, tot wanneer, welke namespace | de `runs`-tabel, en die blijft staan | Al goed geregeld |

## Groep B: wat de beheerder van het platform wil weten

| Gebeurtenis | Ontstaat in | Veroorzaker | Niveau | Context op dat moment | Gaat vandaag heen naar | Willen weten |
|---|---|---|---|---|---|---|
| Een gebruiker die niet op de allowlist staat probeerde binnen te komen | `opi/middleware/authorization.py:117` | mens | platform | e-mailadres, pad | `logger.warning`, daarna een 302 naar `/permission-denied` | Ja, hoog |
| Een API-sleutel of bearer-token werd geweigerd | `opi/api/endpoint_util.py:52`, `:69`, `:135`, `:176` en `opi/api/user_token_auth.py:252` | agent | platform | de routenaam; **niet** het IP-adres, **niet** het project | `logger.warning` | Ja, hoog |
| Inloggen en uitloggen | Keycloak, niet OPI | mens | platform | Keycloak-auditevents | de realm-configuraties zetten `eventsEnabled`, `eventsExpiration: 7776000` (90 dagen) en `adminEventsEnabled` aan (`opi/configs/keycloak/bootstrap.yaml:19-26`, `opi/configs/keycloak/sso-support.yaml:44-51`, en vier andere), sinds commit `b503436e` van 20 juli 2026 | Ja, hoog |
| Een opstartfase van OPI is mislukt en wordt herprobeerd | `opi/core/startup.py:663` (`_startup_retry_loop`) | scheduler | platform | welke fase, welke fout | logregels plus de statuspagina uit `opi/core/readiness.py` | Ja |
| De CAA-records of de no-mail-records op onze DNS-zones weken af | `opi/core/caa_reconciler.py:69`, `opi/core/no_mail_reconciler.py:113`-`:155` | buiten | platform | zone, naam, aangetroffen record | `logger.warning`; zie `features/caa-records.md` en `features/no-mail-dns-records.md` | Ja |
| Een databaseconsole of jobbundel is verlopen en opgeruimd | `opi/core/db_console_reaper.py:157`, `:194`, `:229` | scheduler | project | sessie, namespace | logregels plus een eindtoestand in `runs` | Deels geregeld |
| Een taak is vastgelopen en door de stale-recovery teruggezet | `opi/core/task_worker.py` (heartbeat op `:250`, hersteltak) | scheduler | project | taak, poging, laatste hartslag | de `async_tasks`-rij, en die verdwijnt na een uur | Ja |
| Een in-memory wizardtaak hing twee uur zonder afronding en is weggegooid | `opi/core/task_manager.py:256` | scheduler | project | project, aanmaaktijd | `logger.warning`, en dan weg | Ja |
| Een out-of-band bewerking van `zad-projects` is opgepikt door de trage reconcile-poll | `opi/services/project_store.py:1343` | buiten | platform | wat er veranderde | niets bij succes; alleen een `logger.error` bij een fout | Ja |
| Een federatietaak is doorgestuurd naar een ander cluster | `opi/core/federation_service.py:60` en `:90` | mens of agent | project | doorcluster, taaksoort | een taak op het doelcluster; zie `features/federation-routing.md` | Ja |
| De reconciliatie sloeg over omdat de projectstore leeg was | `opi/core/reconciliation_scheduler.py:96` | scheduler | platform | niets | `logger.warning` | Ja, hoog |
| Er is een bootstrapwijziging gecommit die het cluster nooit bereikt heeft | niet in code; `bootstrap/rig-system/kustomize/overlays/odcn-production` wordt met de hand toegepast via `task bootstrap-argo-system` | mens | platform | het verschil tussen gerenderde bootstrap en live toestand | niets; bewezen op 21 augustus 2026 (`TODO.md` punt 27) | Ja, zeer hoog |
| OPI zelf logt een ERROR of CRITICAL | overal | alle | platform | de logregel | de log watcher grept ze uit Loki, filtert tegen `opi/services/log_watch_ignore_patterns.txt` (91 regels) en duwt de rest naar ntfy; zie `features/log-watcher.md` | Deels geregeld |
| Een gedeeld cluster raakt vol, of een subprocess vreet geheugen | `opi/core/metrics.py` (`OPICollector`) | cluster | platform | Prometheus-gauges | `/metrics`, zonder alerteringsregels (zie hieronder) | Ja |

## Groep C: wat een agent of script op de API wil weten

De agent is vandaag structureel het slechtst bediend, en om een reden die niet in de lijst zichtbaar is: hij is niet te onderscheiden van elke andere houder van dezelfde sleutel. Een taak die via de API is gestart krijgt `created_by = "API"` (`opi/core/task_helpers.py:63`), letterlijk die string, want de sleutel identificeert het project en niet de handelende partij.

| Gebeurtenis | Waar de agent hem vandaag vandaan haalt | Wat daaraan schort |
|---|---|---|
| Mijn taak is klaar of mislukt | pollen op `GET /api/v2/.../tasks/{id}` tot de status verandert | Alleen als hij de taak zelf startte; en na een uur is de rij weg |
| De deployment die ik zojuist bijwerkte is nu gezond | pollen op `GET /api/v2/projects/{p}/deployments/{d}`, die `errors` en `deviations` teruggeeft | Elke bevraging herberekent alles; er is geen "sinds wanneer" en geen push |
| Mijn image is uitgerold | pollen | Geen signaal; `features/upsert-deployment-api.md` beschrijft alleen de heenweg |
| Er is iets aan mijn project veranderd door iemand anders | de commits in `zad-projects` lezen | Kan alleen wie leestoegang tot de repo heeft; en de auteur is altijd dezelfde |
| Mijn project heeft een quotum of grens geraakt | nergens | Bestaat niet |

Er is dus geen enkel push-kanaal richting een agent, en het enige pull-kanaal is een taakstatus met een uur bewaartijd.

---

## Achtergrondprocessen die alleen loggen

Dit is de volledige lijst uit de lifespan van `opi/server.py`, in startvolgorde, aangevuld met de lussen die daarbuiten beginnen. Elk van deze schrijft zijn uitkomst uitsluitend naar de logger, tenzij anders vermeld.

| # | Achtergrondproces | Gestart op | Module | Waar de uitkomst heen gaat |
|---|---|---|---|---|
| 1 | Probeserver op een eigen besturingssysteemdraad | `opi/server.py:93` | `opi/core/probe_server.py` | Alleen het HTTP-antwoord; leest `get_readiness_state()` |
| 2 | Prometheus-collectors en piekgeheugenbemonstering | `opi/server.py:98-99` | `opi/core/metrics.py:316`, `:367` | `/metrics` |
| 3 | tracemalloc, als `ENABLE_TRACEMALLOC` | `opi/server.py:100-101` | `opi/core/metrics.py:324` | `/metrics` |
| 4 | OpenTelemetry-tracing | `opi/server.py:106` | `opi/core/tracing.py:21` | Niets: `OTEL_ENABLED: bool = False` (`opi/core/config.py:272`) |
| 5 | Opstarttaken, met een hersteldraad die elke 60 seconden opnieuw probeert | `opi/server.py:113` | `opi/core/startup.py:719`, `:663` | Logger plus `opi/core/readiness.py` |
| 5a | CAA-reconciliatie op onze DNS-zones | binnen 5 | `opi/core/caa_reconciler.py` | Logger |
| 5b | No-mail-reconciliatie (SPF, null-MX, DMARC) op de routernamen | binnen 5 | `opi/core/no_mail_reconciler.py` | Logger |
| 5c | Platform-mailaccount op de relay klaarzetten | `opi/core/startup.py:438` | `opi/manager/mail_manager.py:248` | Logger plus een Secret in de eigen namespace |
| 5d | Prometheus-herverbindingslus | `opi/core/startup.py:751` | `opi/core/startup.py:188` | Logger |
| 6 | Git-monitor op het projectbestand | `opi/server.py:118` | `opi/core/git_monitor.py`, wrapper op `opi/connectors/git.py:2137` | Logger |
| 7 | Periodieke opruiming van in-memory wizardtaken, elke 300 seconden | `opi/server.py:124` | `opi/core/task_manager.py:277` | Logger |
| 8 | Trage reconcile-poll van de ProjectStore | `opi/server.py:129` | `opi/services/project_store.py:1343` | Logger, alleen bij fouten |
| 9 | TaskWorker, met hartslaglus en stale recovery | `opi/server.py:214` | `opi/core/task_worker.py` | Tabel `async_tasks`, opgeruimd na een uur |
| 10 | BackupScheduler | `opi/server.py:223` | `opi/core/backup_scheduler.py` | `BACKUP`-taken plus logger |
| 11 | Retentiesweep over de backups, binnen de backupscheduler | `opi/core/backup_scheduler.py:210-212` | `opi/core/backup_retention_sweep.py` | Logger |
| 12 | ResourceTuningScheduler, de nachtelijke VPA-tuner | `opi/server.py:236` | `opi/core/resource_tuning_scheduler.py` | `resources.history` in het projectbestand plus logger |
| 13 | ReconciliationScheduler, de nachtelijke opruiming | `opi/server.py:247` | `opi/core/reconciliation_scheduler.py`, `opi/jobs/reconciliation.py` | Eén logregel met tellingen |
| 14 | FederationService, alleen in master-modus | `opi/server.py:280` | `opi/core/federation_service.py` | Taken op het doelcluster plus logger |
| 15 | DbConsoleReaper, de run-reaper | `opi/server.py:293` | `opi/core/db_console_reaper.py` | Tabel `runs` plus logger |
| 16 | LogwatcherScheduler | `opi/server.py:304` | `opi/core/logwatcher_scheduler.py`, `opi/services/log_watcher.py` | ntfy plus logger |
| 17 | SleepModeScheduler | `opi/server.py:315` | `opi/services/catalog/sleep_mode/scheduler.py` | `SLEEP_DEPLOYMENT`-taken, het projectbestand, plus logger |
| 18 | OOM- en gezondheidswatcher, fire-and-forget na elke uitrol | `opi/services/oom_watcher.py:704` | `opi/services/oom_watcher.py` | Taken plus logger; zie `features/oom-kill-watcher.md` |
| 19 | Verbindingsherstel van de kubectl-connector | `opi/connectors/kubectl.py:120` en `:168` | `opi/connectors/kubectl.py` | Logger |

Ten opzichte van het startpunt van de opdracht zijn de nummers 1, 2, 3, 5c, 5d, 7, 9, 11, 14, 15 en 19 toegevoegd, en is nummer 4 gepreciseerd (aanwezig maar uit).

### Twee correcties op het startpunt

**De git-monitor slikt schemafouten niet meer stil.** Het startpunt zegt dat validatiefouten daar stil worden geslikt. Dat was zo en is het niet meer: `opi/core/git_monitor.py:144-150` vangt `ProjectSchemaError` en schrijft `logger.error("Projectbestand ... afgekeurd door schemavalidatie en NIET verwerkt: ...")`. Het commentaar erboven noemt de aanleiding: "the old silence meant 22 production files were being skipped here with nobody able to see it". Een logregel is nog steeds geen gebeurtenis, en de gebruiker van dat project ziet er nog steeds niets van, maar de stilte is weg.

**De Keycloak-auditevents zijn wel gecommit.** Het startpunt zegt dat ze niet gecommit zijn. Ze staan in zes realm-configuraties, toegevoegd in commit `b503436e` van 20 juli 2026, met de post-mortem als expliciete aanleiding in het commentaar. Wat wél klopt is dat ze op bestaande productierealms waarschijnlijk niet aan staan, en de precieze oorzaak staat in `plans/technische-review-bio-en-nora-bevindingen.md`: `opi/connectors/keycloak.py:203-210` herhaalt de instellingen alleen op een 409 uit `create_realm()`, en de reconcile-weg voor een reeds bestaand projectrealm roept `create_realm()` helemaal niet aan. Elk realm dat vóór 20 juli 2026 is aangemaakt heeft de instelling dus nooit met terugwerkende kracht gekregen. Of dat op productie inderdaad zo is, is **niet geverifieerd**: daarvoor is een blik op het draaiende cluster nodig.

---

## Toestand die wel wordt bepaald maar niet als gebeurtenis bestaat

- **Gezondheid en afwijkingen van een deployment.** `gather_deployment_errors` en `gather_sync_deviations` in `opi/services/deployment_diagnostics.py` bouwen bij elk paginabezoek opnieuw een lijst `errors` en `deviations` op uit de ArgoCD-status en het cluster. Er wordt niets bewaard, dus "sinds wanneer is dit rood" is onbeantwoordbaar, en "het was gisteren ook al rood" is niet vast te stellen. Zie `features/deployment-state-and-health.md` en `features/status-afwijkingen.md`.
- **Toestand die een dienst zelf bijdraagt.** `DeploymentStateFact` (`opi/services/catalog/base.py`, geproduceerd door `opi/services/catalog/deployment_health/__init__.py:60` en `opi/services/catalog/sleep_mode/__init__.py:75`) is een antwoord op de vraag "wat weet jij over deze deployment", berekend uit het projectbestand. Ook dat is een momentopname.
- **ArgoCD sync- en healthovergangen.** OPI leest ze (`opi/core/simple_background.py`, `features/argocd-sync-wait.md`), maar bewaart alleen de eindstand in een taak. De overgang zelf, en dus de duur van een storing, bestaat nergens.
- **Renderfouten van ArgoCD.** `features/argocd-render-error-surfacing.md` beschrijft hoe ze zichtbaar worden gemaakt; ze worden niet bewaard.
- **Het onderscheid tussen een probe-kill en een echte crash.** `features/probe-kill-is-geen-crash.md` beschrijft de logica; de uitkomst wordt getoond, niet bewaard.

---

## Wat er aan meld- en exportinfrastructuur ligt

| Voorziening | Toestand | Bewijs |
|---|---|---|
| **ntfy** | In gebruik, één publiek: het platformteam | `opi/core/logwatcher_scheduler.py`, `opi/services/log_watcher.py:327` (`send_ntfy`) |
| **De eigen mailrelay (Stalwart)** | Draait, en ZAD heeft er een eigen account op | `opi/connectors/mail.py`, `opi/manager/mail_manager.py:248` (`ensure_platform_account`), aangeroepen vanuit `opi/core/startup.py:438` |
| **Mail versturen vanuit OPI** | Bestaat niet | Er is geen enkele aanroep van `smtplib`, `aiosmtplib` of een SMTP-client in `opi/`; de relay wordt alleen via zijn beheer-API benaderd. De docstring van `ensure_platform_account` zegt zelf dat dit account is "what unblocks password reset and invite mail" |
| **Naar buiten mailen** | Werkt niet | Gemeten op 21 augustus 2026: een adres binnen `rijksoverheid.nl` krijgt `250 ok`, een extern adres een `550 #5.1.0 Address rejected` bij `rmrmail.rijksweb.nl` (`TODO.md` punt 26) |
| **OpenTelemetry** | Volledig aanwezig, uit | Negen `opentelemetry-*`-afhankelijkheden in `operations-manager/python/pyproject.toml:66-74` met instrumentatie voor FastAPI, httpx, aiohttp, asyncpg, SQLAlchemy en logging; `opi/core/tracing.py`; `OTEL_ENABLED: bool = False` |
| **Een OTLP-ontvanger** | Bestaat niet in deze repo | `OTEL_EXPORTER_OTLP_ENDPOINT` wijst naar `http://jaeger.rig-system:4317` (`opi/core/config.py:274`), maar er staat geen Jaeger in `infrastructure/bootstrap/infrastructure/` |
| **De exporter zelf** | Alleen traces | `opi/core/tracing.py` importeert uitsluitend `OTLPSpanExporter`; er is geen log- of metriekexporter |
| **Prometheus** | Draait, exporteert alleen procesinterne toestand van OPI | `opi/core/metrics.py`: uitsluitend `GaugeMetricFamily`, geen enkele `Counter` voor een domeingebeurtenis |
| **Alertmanager en alerteringsregels** | Bestaan niet | `infrastructure/bootstrap/infrastructure/prometheus/controller/base/configmap.yaml` heeft alleen `scrape_configs`, geen `rule_files` en geen `alerting`; er is geen bestand in `infrastructure/` of `bootstrap/` dat het woord alertmanager noemt |
| **Loki, Grafana en Mimir** | Buiten deze repo, wel in gebruik | `GRAFANA_URL` wijst naar `grafana-service.rig-system.svc.cluster.local:3000` (`opi/core/config.py:434`), met `GRAFANA_TOKEN` en `GRAFANA_DATASOURCE_UID` ernaast (`:435-437`); de productie-configmap zet `GRAFANA_DATASOURCE_UID=mimir-prd` en `GRAFANA_BILLING_DATASOURCE_UID=mimir-billing` (`bootstrap/rig-system/kustomize/operations-manager/overlays/odcn-production/configmap.yaml:47-48`). Er staat geen Loki-, Grafana- of Mimir-component in `infrastructure/`: de stack wordt geleverd, niet beheerd |
| **Kubernetes-events** | Per taak opgehaald | `opi/connectors/kubectl.py:969` (`get_namespace_events`), weggeschreven in de `events`-kolom van `async_tasks` via `opi/core/persistent_task_progress.py:137` |
| **De dienst-eventregistry** | Een dispatchmechanisme, geen geschiedenis | `opi/services/services_enums.py:149` (`ActionEvent`, twee waarden) en `:184` (`UIEvent`, drie waarden); zie `features/service-event-hooks.md` |

Dat de enige bestaande meldketen (log watcher naar ntfy) afhangt van een Loki en een Grafana die niet in deze repo staan, is op zichzelf een risico: de keten is niet reproduceerbaar op te bouwen vanuit dit versiebeheer.

---

## De gebeurtenissen die vandaag verloren gaan en het duurst zijn om te missen

Zeven, geordend naar wat het al heeft gekost of aantoonbaar had kunnen kosten.

**1. Wie deed wat, bij een beveiligingsincident.** De post-mortem `docs/post-mortems/user-impersonation-oidc-email-claim.md` beschrijft een lek waarmee iedereen die via SSO-Rijk kon inloggen zich als een ander kon voordoen, in het ergste geval als beheerder. De publieke melding aan gebruikers (`docs/post-mortems/melding-zad-gebruikers.md`) zegt: "We hebben op dit moment geen aanwijzing dat het is misbruikt, en we onderzoeken nog of we dat sluitend kunnen onderbouwen." In de tijdlijn staat één regel op `[loopt]`: "Controle toegang tot Wies/ZAD/Keycloak wijzigingen". Dat onderzoek is precies wat een gebeurtenissenlog beantwoordt en wat vandaag onbeantwoordbaar is: de commits dragen één identiteit, de tabel die de actor draagt wordt na een uur geleegd, en de Keycloak-auditevents stonden op bestaande realms nog niet aan. Dit is de duurste, want de kosten zijn al gemaakt.

**2. Een projectbestand dat weken op een schemafout strandt.** Het commentaar in `opi/core/git_monitor.py:144-149` benoemt het zelf: "the old silence meant 22 production files were being skipped here with nobody able to see it". Er is nu een ERROR-logregel, en die haalt via de log watcher zelfs ntfy, maar de gebruiker van dat project ziet er niets van. Zolang de gebruiker het niet ziet is de weg terug een support-gesprek.

**3. Een bezorging die stil verdwijnt.** `TODO.md` punt 26, onderdeel 2, met de meting erbij: de relay adresseert de DSN aan `noreply-rijksapp+<project>@rijksoverheid.nl`, de upstream weigert dat adres ook, de relay noteert "discarding message after double bounce" en gooit het weg, en de relaylog bewaart drie uur. Het punt zegt letterlijk: "Een project hoort dus niets, wij ook niet". Het voorgestelde noodverband in dat punt is een alert op een mislukte bezorging, en dat is een gebeurtenis.

**4. Een commit die het cluster nooit bereikt.** `TODO.md` punt 27, bewezen op 21 augustus 2026: PR #168 zette de mailrelay uit "tot de RCA rond is", en die commit heeft het cluster nooit bereikt omdat `bootstrap/rig-system/kustomize/overlays/odcn-production` met de hand wordt toegepast. OPI wees die hele periode naar de crashende relay terwijl iedereen dacht dat de dienst uit stond. Het punt eindigt met een open beslissing: "waar de melding landt". Ook dat is een gebeurtenis.

**5. Data die 's nachts verdwijnt.** De reconciliatiescheduler purget databases, buckets en PVC's die uit een projectbestand zijn verdwenen (`opi/core/reconciliation_scheduler.py`, `opi/jobs/reconciliation.py`, zie `features/service-orphan-reconciliation.md` en `features/yaml-diff-driven-deletion.md`). De uitkomst is één logregel met drie tellingen, en de rij in `marked_for_deletion` die het voornemen droeg verdwijnt bij het opruimen. Er blijft dus niets over dat zegt dat er iets is weggegooid, laat staan wat.

**6. Een grens die vannacht is verhoogd.** De resource-tuner en de OOM-watcher wijzigen zelfstandig geheugen- en CPU-grenzen. Dit is het enige onderwerp waar wél iets bewaard blijft (`resources.history` in het projectbestand, mét reden), en het laat precies zien waar het aan schort: geen actor, alleen zichtbaar voor wie het projectbestand openslaat, en niet bevraagbaar over projecten heen.

**7. Een deployment die al drie dagen rood is.** Omdat `errors` en `deviations` per paginabezoek worden herberekend, is er geen enkele manier om vast te stellen hoe lang iets al stuk is, en dus ook geen manier om te merken dat niemand ernaar heeft gekeken.

---

## Wat niet is geverifieerd

- Of de Keycloak-auditevents op de draaiende productierealms aan staan. Dat vraagt een blik op het cluster.
- Of de standaardretentie van een uur op `async_tasks` op productie inderdaad geldt. Er staat geen `TASK_WORKER_CLEANUP_RETENTION_HOURS` in `bootstrap/rig-system/kustomize/`, dus de standaardwaarde uit `opi/core/config.py:369` is de waarschijnlijke waarde, maar een omgevingsvariabele elders kan hem alsnog zetten.
- De retentie van de externe Loki-installatie en de node-log-rotatie. `plans/technische-review-bio-en-nora-bevindingen.md` noemt ongeveer drie uur voor de node-log-rotatie en stelt vast dat beide buiten deze repo staan en niet te verifiëren zijn. De drie uur uit `TODO.md` punt 26 gaat over de relaylog, niet over de OPI-log.
- Of er buiten dit versiebeheer nog een meldkanaal bestaat (een dashboard, een handmatige Grafana-alert). Binnen de repo is ntfy het enige.
