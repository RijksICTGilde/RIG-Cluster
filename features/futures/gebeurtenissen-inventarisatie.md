# Gebeurtenissen in ZAD: de inventaris

## Status en meetbasis

Dit is deel 1 van drie. Het beschrijft alleen wat er is; de oplossingsrichtingen staan in `features/futures/gebeurtenissen-vastleggen-en-melden.md` en het plan in `features/futures/gebeurtenissen-plan-van-aanpak.md`. Er is voor deze inventaris geen code gewijzigd.

| Deel | Document |
|---|---|
| 1. Welke gebeurtenissen hebben we | dit document |
| 2. Hoe leggen we ze vast, en hoe melden we | `features/futures/gebeurtenissen-vastleggen-en-melden.md` |
| 3. De kanalen, de fasering en de open beslissingen | `features/futures/gebeurtenissen-plan-van-aanpak.md` |

**Herkomst.** Dit document is de samenvoeging van twee onafhankelijk geschreven inventarisaties over hetzelfde onderwerp: het inmiddels verwijderde `plans/meldingen-inventarisatie.md` (geschreven op 22 augustus 2026) en de eerdere versie van dit bestand (geschreven op 21 augustus 2026). Beide zijn gemeten tegen commit `83ac4b9b`. De eerste leverde de catalogus van gebeurtenissen met codeankers, ernst en standaardkanaal; de tweede leverde de indeling naar publiek (de drie groepen zelf, niet de waarde per catalogusrij; zie hieronder), de bestaande administraties, de achtergrondprocessen, de meld- en exportinfrastructuur en de rangschikking naar wat het duurst is om te missen. De samenvoeging is gedaan in RC-163 op 28 augustus 2026 en heeft de ankers niet opnieuw gemeten; wat hieronder staat is wat de twee bronstukken hebben gemeten, met de dubbelingen samengevouwen en de tegenspraken beslecht (die zitten in deel 2 en 3, niet in dit document). Op twee punten gaat de catalogus verder dan wat de bronstukken hebben gemeten, en dat zijn de enige twee. Het eerste en veruit grootste is de kolom `publiek`. Die staat hieronder op alle negenenzeventig catalogusrijen, en geen van beide bronstukken kan hem voor meer dan een deel geleverd hebben: de meldingenset had de kolom niet (de kop van al zijn negen catalogustabellen luidt `event | bron | onderwerp | belanghebbenden | ernst | standaardkanaal`, en die negen tabellen leverden vierenzestig van de negenenzeventig rijen), en de gebeurtenissenset drukte publiek niet als kolom uit maar als sectielidmaatschap: zestien regels onder groep A, veertien onder groep B en vijf onder groep C, samen vijfendertig regels die elk precies EEN groep noemen. Er kunnen dus hoogstens vijfendertig rijen hun publiek uit een bronstuk hebben geerfd, en voor de overige minstens vierenveertig is de waarde in RC-163 TOEGEKEND. Achtentwintig rijen dragen bovendien een gecombineerde waarde die geen enkel bronstuk kan leveren (twaalf keer `A en B`, zestien keer `A en C`), want in de bronstukken noemt elke regel precies een groep. Van de vijfendertig bronregels zijn er zeventien op ankeroverlap aan een catalogusrij te binden, samen eenentwintig paren; twintig van die paren komen overeen, en het ene dat dat niet doet is `:328`, dat hieronder bij paragraaf 8 als correctie op het bronstuk is verantwoord. Het tweede punt is kleiner: vijftien van de negenenzeventig catalogusrijen komen uitsluitend uit de gebeurtenissenset (`:141`, `:232`, `:248`, `:270`, `:295` tot en met `:301`, `:344`, `:377` en `:378`, `:385`), en de tabellen daar hadden ook geen kolom belanghebbenden, geen kolom ernst en geen kolom standaardkanaal. Voor die vijftien rijen zijn ook die drie waarden in RC-163 zelf TOEGEKEND, naar het patroon van hun buren in dezelfde groep, met een uitzondering: `:344` is de enige rij van paragraaf 9 en heeft dus geen buren, en daar zijn de drie waarden afgeleid uit wat die paragraaf zelf meet. Standaardkanaal `ntfy` omdat de pijplijn daar vandaag al naartoe POST (`log_watcher.py:327`); belanghebbende de platformbeheerder omdat de infrastructuurtabel van dit document op regel `:448` vaststelt dat het platformteam het publiek van dat ntfy-onderwerp is; ernst `actie nodig` omdat de bewaker uitsluitend ERROR- en CRITICAL-regels oppikt (`log_watcher.py:16`). En paragraaf 9 zegt op regel `:357` van dit document zelf dat dit publiek `B` is en niet `A`. Geen van deze waarden is dus gemeten of uit een bronstuk overgenomen. Waar hieronder staat dat een van deze kolommen is gecorrigeerd, gaat het bij die vijftien rijen om een correctie binnen RC-163 en niet om een correctie op een bronstuk.

**De meetbasis.** Alles hieronder is gemeten op commit `83ac4b9b` van 21 augustus 2026, de tip van de ontwikkellijn waar `release-augustus-2026` deel van uitmaakt. Dat was toen bewust niet `main`: `main` stond op `51fd763e` van 27 juli 2026 en liep 1658 commits achter, en drie van de documenten waar dit stuk naar verwijst (`features/deployment-state-and-health.md`, `features/status-afwijkingen.md`, `features/service-event-hooks.md`) bestonden daar nog niet, net zomin als zes van de drieentwintig achtergrondprocessen uit de lijst verderop. Op de tak waar dit document nu op staat is `83ac4b9b` een voorouder van de tip en bestaan die drie documenten wel, dus die beslissing is inmiddels genomen en de verwijzingen kloppen. Wie een regel hieronder wil natrekken en hem niet vindt, controleert eerst op welke tak hij staat: `git show 83ac4b9b:<pad>` werkt altijd.

**Padconventie.** Een pad dat met `opi/`, `tests/` of `manifests/` begint is relatief aan `operations-manager/python/`; dat is de wortel van het Python-pakket. Elk ander pad is relatief aan de wortel van de repository. Dat onderscheid is nodig omdat de repository zelf ook een `docs/`, een `scripts/` en een `plans/` heeft.

Regels die niet in code te controleren waren staan gemarkeerd als **niet geverifieerd**. Zelfbedachte namen komen in dit document alleen als voorstel voor, en op twee plekken: de vijf taakgroepen in paragraaf 1, en de twaalf typen onder "De groepering naar type" onderaan, in een tabel waarvan de kopkolom letterlijk `Type (VOORSTEL)` heet en waarin die vijf opgaan. Uit die twaalf worden namen in deel 2 en deel 3 als identifier aangehaald. De overige zelfbedachte namen staan in deel 2 en deel 3 en zijn daar op dezelfde manier als VOORSTEL gemarkeerd.

## Woordkeuze

Het woord *event* is in deze codebase al twee keer bezet. Dit document gebruikt daarom consequent **gebeurtenis** voor het derde begrip: iets dat op het platform is gebeurd en dat het waard is te bewaren en mogelijk te melden. De volledige afweging en wat dat voor `ActionEvent`, `UIEvent` en de `events`-kolom betekent staat in deel 2 onder "De begripsbotsing, en het besluit".

## De stand van zaken in een alinea

ZAD heeft geen meldingssysteem. Gemeten: er is geen tabel, geen model, geen route en geen sjabloon dat er over gaat. `grep -rn notification --include="*.py" opi/` levert **elf treffers in zes bestanden** op, en die gaan alle over de logbewaker en ntfy (`opi/services/log_watcher.py`, `opi/core/logwatcher_scheduler.py`, `opi/core/config.py:440`, `opi/core/simple_background.py:117`) of over het contactadres voor de certificaten van Let's Encrypt (`opi/api/router.py:1012`, `opi/core/cluster_config.py:1009`).

Laat je de beperking tot `.py` weg (`grep -rn notification opi/`), dan zijn het **achttien treffers in acht bestanden**: er komen zes regels bij in `opi/templates_lotc/bg/feedback.html.j2` en een in `opi/services/log_watch_ignore_patterns.txt`. Die zes zijn de LOTC-componenten `c-notification` en `c-notification-item` op de proefopstelling, en dat is een *vluchtige bevestiging na een actie*, geen postvak; de naamsbotsing staat in deel 3 onder Kanaal 1. De conclusie verandert er niet door: er is niets om op voort te bouwen en ook niets om te slopen.

Er zijn vandaag precies **drie manieren** waarop iemand te weten komt dat er iets gebeurd is:

1. **Hij kijkt op het juiste scherm op het juiste moment.** De projectdetailpagina, de deploymentkaart, de takentabel, `/admin/approvals`. Alles is trekwerk: de pagina vraagt, het scherm antwoordt, en wie niet kijkt weet niets.
2. **Hij volgt een taak die hij zelf startte.** Het voortgangsvenster ververst zichzelf met `hx-trigger="every 2s"` (`opi/templates_lotc/partials/task_progress_fragment.html.j2:34`) tot de taak klaar of mislukt is. Dat werkt goed, maar het is gebonden aan het tabblad dat op dat moment openstaat.
3. **Hij is de platformbeheerder die ntfy leest.** De logbewaker (`opi/services/log_watcher.py`) stuurt ERROR- en CRITICAL-regels uit het OPI-log naar een ntfy-topic. Dat is de enige weg waarlangs ZAD vandaag uit zichzelf iets naar buiten duwt.

### Waarom "op het scherm kijken" hier niet genoeg is, in een getal

`TASK_WORKER_CLEANUP_RETENTION_HOURS` staat op **1** (`opi/core/config.py:369`). De opruimlus van de takenwerker (`opi/core/task_worker.py:420`) verwijdert elke taak in een eindtoestand waarvan `completed_at` ouder is dan dat venster (`opi/core/async_task_service.py:670`). Een deployment die vannacht om drie uur mislukte, bestaat om vier uur nergens meer: niet in de takentabel, niet in de API, nergens. De uitzondering die er is bewijst dat het probleem bekend is: een uitgestelde uitrol wordt expliciet gespaard, "want drift die na een week verdwijnt is precies de stille drift die dit moet laten zien". Dezelfde redenering geldt voor een mislukte deploy, en daar geldt hij niet.

**Dat is de kern van de opdracht.** Het gaat niet alleen om aflevering per e-mail of Mattermost. Het gaat er eerst om dat een gebeurtenis ergens BLIJFT staan, lang genoeg dat iemand hem de volgende ochtend nog kan zien.

## Hoe je de tabellen leest

| Kolom | Betekenis |
|---|---|
| gebeurtenis | wat er gebeurt, in gewone taal |
| bron | het codeanker waar hij vandaag ontstaat, of **bestaat nog niet** |
| onderwerp | waar hij over gaat: project / deployment / component / dienst / gebruiker / platform |
| belanghebbenden | wie er iets aan heeft, in rollen |
| publiek | A, B of C, zie hieronder |
| ernst | ter informatie / actie nodig / storing |
| standaardkanaal | wat een redelijke standaard is, en waarom |

### De drie publieken

De kolom `publiek` is de dwarsdoorsnede die de rest van het plan draagt: de drie publieken hebben andere lijsten, andere drempels en andere kanalen. Hij staat bewust als kolom en niet als een tweede lijst naast deze, want twee lijsten over dezelfde gebeurtenissen lopen gegarandeerd uit de pas.

- **A: de gebruiker van een project.** Wil weten wat er met zijn eigen project gebeurde, ook als hij er niet bij was. Zijn horizon is een project.
- **B: de beheerder van het platform.** Wil weten of het platform gezond is en wie wat deed. Zijn horizon is het cluster.
- **C: de agent of het script op de API.** Wil een machineleesbaar signaal om op te reageren in plaats van te pollen. Zijn horizon is een project, maar via een sleutel in plaats van een sessie.

Een gebeurtenis kan meer dan een publiek hebben; `A en B` betekent dat beide hem willen, elk met een eigen drempel. `C` staat er alleen bij waar een agent er vandaag aantoonbaar op zou reageren; wat een agent vandaag wel en niet kan zien staat verderop in een eigen paragraaf, want dat gaat over het mechanisme en niet over de gebeurtenissen.

### De veroorzakers

De veroorzakers zijn: **mens** (een sessie in de portal), **agent** (een API-sleutel of bearer-token), **scheduler** (een achtergrondlus van OPI zelf), **cluster** (kubelet, ArgoCD, CNPG) en **buiten** (een directe push naar git, een upstream mailserver, een DNS-houder). Waar de veroorzaker niet uit de bronkolom volgt, staat hij in de tekst onder de tabel.

### "Bestaat nog niet" heeft twee smaken

Het verschil is belangrijk voor het bouwwerk:

- *de toestand bestaat, de gebeurtenis niet*: de code weet het moment wel, maar er is geen plek waar hij het meldt. Bijvoorbeeld een mislukte taak: de status gaat naar `failed`, en daar houdt het op. Dit is goedkoop: er hoeft alleen een aanroep bij.
- *de toestand bestaat ook niet*: er is niets dat het waarneemt. Bijvoorbeeld een certificaat dat bijna verloopt. Dit is duur: er moet eerst iets gebouwd worden dat kijkt.

Waar het uitmaakt staat het erbij. Dit onderscheid draagt de kostenschatting van het hele plan: het is het verschil tussen een regel code en een eigen opdracht.

### De rollen

Uit de code, niet verzonnen:

| Rol | Waar hij vandaan komt |
|---|---|
| **platformbeheerder** | `UserService.is_platform_admin()`, een allowlist op e-mailadres |
| **projectbeheerder** | rol `admin` of `owner` in het projectbestand; `PROJECT_EDIT_ROLES` in `opi/services/project_authorization.py:27` |
| **projectlid** | rol `member` of `developer`; het schema kent vier rollen (`admin`, `owner`, `member`, `developer`, `opi/schemas/project_v2.json`) |
| **actor** | degene die de handeling zelf uitvoerde. Vaak identiek aan een van de andere rollen, maar apart genoemd omdat "jouw eigen actie is klaar" een andere melding is dan "er is iets met jouw project gebeurd" |

Let op: de rol **actor** is vandaag maar half vastgelegd. Taken dragen `created_by` (`opi/core/async_task_service.py:113`), runs dragen `started_by` en `ended_by` (`opi/services/persistence/runs.py`), maar een goedkeuringsoordeel legt alleen `by` in de history vast en een wijziging aan het projectbestand landt in de git-commit onder een vaste systeemidentiteit. Wie iets deed is dus per bron ergens anders opgeslagen, en op een van die plekken helemaal niet; zie "De blinde vlek die alles hieronder kleurt".

### De standaardkanalen die in de tabellen voorkomen

`postvak` is het meldingenoverzicht in ZAD zelf (bestaat nog niet, zie deel 3). `mail` is e-mail naar de persoon. `ntfy` is het bestaande beheerderskanaal. `geen` betekent: wel vastleggen, niet actief melden; je ziet het als je gaat kijken. Waar "postvak + mail" staat is de mail bedoeld als standaard AAN voor die rol, met de mogelijkheid hem uit te zetten.

---

## Wat vandaag al persistent is

Het startpunt van de opdracht zegt dat er twee tabellen zijn die dit werk al doen. Er zijn er vijf, plus drie geschiedenissen die in het projectbestand zelf staan. Dat is de belangrijkste correctie op dat startpunt: naast bestaande administratie een derde beginnen is niet het risico; het risico is naast **acht** bestaande administraties een negende beginnen.

| Wat | Waar | Sleutelvelden | Wat het niet draagt |
|---|---|---|---|
| Taken, 23 soorten (`TaskType`) met zes toestanden (`AsyncTaskStatus`) | tabel `async_tasks`, `opi/core/async_task_schema.py`, `opi/core/async_task_service.py:54` | `project_name`, `deployment_name`, `cluster`, `created_by`, `current_step`, `progress_percent`, `subtasks`, `logs`, `events`, `web_addresses`, `result`, `error_message`, `attempt_count`, `created_at`/`started_at`/`completed_at` | Wordt standaard na **1 uur** verwijderd (`TASK_WORKER_CLEANUP_RETENTION_HOURS: int = 1`, `opi/core/config.py:369`, gebruikt in `opi/core/task_worker.py:426`) |
| Runs, de tijdelijke databaseconsoles en jobbundels | tabel `runs`, `opi/core/runs_schema.py` | `kind`, `status`, `started_by`, `ended_by`, `expires_at`, `error_message`, `namespace`, `spec` | Alleen deze ene soort werklast; de docstring noemt zichzelf letterlijk "the administration/history record" en "the audit trail" |
| Platformgebruikers | tabel `users`, `opi/core/user_schema.py` | `email`, `full_name`, `created_at`, `updated_at` | Wie de gebruiker aanmaakte of wijzigde; alleen de laatste toestand, geen historie |
| Gemarkeerd voor verwijdering | tabel `marked_for_deletion`, `opi/core/marked_for_deletion_schema.py` | `resource_type`, `resource_name`, `project_name`, `deployment_name`, `cluster`, `marked_at`, `metadata` | Wie de markering veroorzaakte; en de rij verdwijnt zodra de reconciliatie opruimt, dus er blijft niets over dat zegt dat er iets is opgeruimd |
| Subdomeinregister | tabel `subdomain_registry`, `opi/services/persistence/subdomain_registry.py:737` | `subdomain`, `base_domain`, `project_name`, `deployment_name`, `cluster`, `created_at`, `created_by` | Alleen de huidige claim; een vrijgegeven subdomein laat geen spoor na |
| Resource-historie per component | projectbestand, `$defs/resource-history-entry` in `opi/schemas/project_v2.json` | `timestamp`, `limits`, `requests`, `source` (`auto-tune`/`oom-watcher`/`manual`), `deployment`, `reason` | Geen actor. Dit is feitelijk al een gebeurtenissenlog voor een domein, met een `reason` in proza |
| Dienstrevisies | projectbestand, `$defs/service-revision` in `opi/schemas/project_v2.json` | `generation`, `resource`, `status`, `created_at`, `superseded_at`, `actions[].type`/`.source`/`.timestamp` | Geen actor; zie `features/service-revision-tracking.md` |
| Kloonstatus | projectbestand, `$defs/clone-from` in `opi/schemas/project_v2.json` | `status.completed`, `status.timestamp` | Een boolean plus een tijdstip; geen uitkomst, geen actor |

Alle overige tabellen bestaan niet: er zijn vier Alembic-migraties (`opi/migrations/versions/001_baseline.py` tot en met `opi/migrations/versions/004_add_runs.py`) en die dekken precies de vijf tabellen hierboven.

### De blinde vlek die alles hieronder kleurt

Elke commit in `zad-projects`, `zad-argo-user-applications` en `zad-deployments` wordt geschreven onder een vaste identiteit: `GIT_COMMIT_AUTHOR_NAME = "Operations Manager"` en `GIT_COMMIT_AUTHOR_EMAIL = "operations-manager@example.com"` (`opi/connectors/git.py:53-54`, doorgegeven als `GIT_AUTHOR_NAME`/`GIT_COMMITTER_NAME` op regel 1831-1834). De commitboodschap beschrijft wat er veranderde, nooit wie het vroeg; zie bijvoorbeeld `opi/manager/project_manager.py:7434` (`Add deployment '<naam>' to project '<naam>'`) en `:7612`. De git-historie vertelt dus wel wat er veranderde en niet wie het deed.

Wie-deed-wat bestaat daarom alleen in `async_tasks.created_by` en `runs.started_by`. En `async_tasks` wordt na een uur opgeruimd. Een technische review van dit punt staat al in `plans/technische-review-bio-en-nora-bevindingen.md`, bevinding E, met dezelfde conclusie en met BIO2 8.15.01 erbij: die overheidsmaatregel schrijft een logregel voor met minimaal actie, object, resultaat, oorsprong, **actor** en tijdstempel, en precies de actor ontbreekt.

Twee dingen die die review noemt zijn ook hier van belang. Ten eerste dragen de logregels lokale Amsterdamse tijd zonder offset (`log_format` in `opi/utils/logging_config.py:48` gebruikt de kale `%(asctime)s`), wat tijdcorrelatie rond de zomertijdovergang foutgevoelig maakt. Ten tweede staat er al een correlatie-identificatie in elke logregel: `flow_id`, uit `opi/core/flow_id.py`, met een voorvoegsel per soort stroom (`req-`, `task-`). Die is er dus al en is niet gekoppeld aan iets dat bewaard blijft.

---
## De catalogus

Tien groepen. De eerste negen zijn de domeinen uit de opdracht; de tiende is wat er verder uit de code kwam.

## 1. Asynchrone taken

**Waar**: `opi/core/async_task_service.py` (`TaskType` op regel 54, `AsyncTaskStatus` op regel 80), uitgevoerd door `opi/core/task_worker.py`. De resultaatmodellen per soort staan in `opi/api/task_models.py`. Documentatie: `features/async-task-system.md`, `features/task-progress-view.md`, `features/task-steps.md`.

Er zijn **23 taaksoorten** en **zes toestanden** (`pending`, `claimed`, `running`, `completed`, `failed`, `cancelled`). Dat is niet 138 meldingen: de meeste overgangen zijn mechaniek en geen nieuws. Wat wel nieuws is:

| gebeurtenis | bron | onderwerp | belanghebbenden | publiek | ernst | standaardkanaal |
|---|---|---|---|---|---|---|
| Een taak is mislukt | `async_task_service.py:329` (`fail_task`); aangeroepen vanuit `task_worker.py:208` | deployment of project, afhankelijk van de soort | actor, projectbeheerder | A en C | storing | postvak + mail |
| Een taak is klaar na eerder te zijn mislukt (herstel) | `async_task_service.py:313` (`complete_task`), in combinatie met `attempt` uit dezelfde tabel | idem | actor, projectbeheerder | A en C | ter informatie | postvak |
| Een langlopende taak is klaar terwijl de aanvrager weg is | `async_task_service.py:313` | idem | actor | A en C | ter informatie | postvak |
| Een taak is afgebroken | `async_task_service.py:399` (`update_task_status`) | idem | actor | A en C | ter informatie | postvak |
| Een taak is vastgelopen en teruggezet door de herstellus | `async_task_service.py:449` (`recover_stale_tasks`), lus in `task_worker.py:405` (hartslag op `:250`) | platform | platformbeheerder | B | actie nodig | ntfy |
| Een wijziging staat al langer klaar zonder uitrol | `async_task_service.py:592` (`get_deferred_rollouts`) | project | projectbeheerder | A | actie nodig | postvak |
| Een in-memory wizardtaak hing twee uur zonder afronding en is weggegooid | `opi/core/task_manager.py:256`; wordt alleen `logger.warning` | project | platformbeheerder | B | actie nodig | ntfy |

**De belangrijkste bevinding hier is niet een gemis maar een houdbaarheidsdatum.** De melding "je deploy is mislukt" is in de code wel te maken (er staat een `failed` met een foutmelding), maar de drager ervan verdwijnt na een uur. Elke oplossing die de taakrij als opslag gebruikt, erft dat uur. **En let op de laatste rij van deze tabel, want daar staat nu `platformbeheerder` en niet `platformbeheerder, actor`.** Dat is geen correctie op een bronstuk. Deze rij is een van de vijftien uit de Herkomst-alinea hierboven: hij staat alleen in de gebeurtenissenset (`features/futures/gebeurtenissen-inventarisatie.md:88` op `e98a7811`), in een tabel zonder kolom belanghebbenden, dus geen van beide bronstukken schreef hier iets. De waarde is in RC-163 toegekend, eerst als `platformbeheerder, actor`, en binnen RC-163 teruggebracht tot `platformbeheerder`. Het criterium daarvoor is wel hetzelfde als bij `:278`, waar het wel om een correctie op het bronstuk gaat. De weggegooide in-memory wizardtaak (`:141`) draagt publiek `B` en niets anders, en zijn standaardkanaal is `ntfy`, dat volgens de legenda hierboven (`:93`) het bestaande BEHEERDERSkanaal is. Onder de bereikgrendel uit deel 2, punt 4 komt hij daarom niet op de projectkant: hij draagt wel een project, maar zijn publiek is uitsluitend `B`, en hij is een van de zes rijen waarop die grendel bijt. In de typentabel hieronder valt hij in type 12 (`:529` legt hem daar uitdrukkelijk neer), en dat type staat op `n.v.t.` voor zowel de projectbeheerder als het projectlid (`:524`). De actor die hier genoemd stond kon deze gebeurtenis dus langs geen enkele weg krijgen: niet op de projecttijdlijn, niet op de projectwebhook, en niet via een voorkeur. Zijn vier buren `:135` tot en met `:138` laten zien waar `actor` wel op zijn plaats staat: die dragen publiek `A en C` en gaan naar het postvak. De rol staat dus alleen waar de gebeurtenis ook echt bij de actor uitkomt, en is weggelaten waar dat niet kan; zie het criterium bij `:278`.

**Welke van de 23 soorten een eigen meldingstype verdienen.** Geen enkele, want het is de groep die het type bepaalt en niet de soort. De tabel hieronder deelt alle 23 in vijf groepen in, ook de drie waarvan de uitkomst alleen bij een mislukking voor iemand anders dan de indiener uitmaakt (`refresh_project` en `refresh_deployment` zijn onderhoud, `configure_service_values` is een instelling opslaan). Die drie staan er toch in, want anders dekken de vijf groepen niet alle 23 en zou een mislukte verversing nergens vallen:

| Groep | Taaksoorten |
|---|---|
| Uitrol | `create_project`, `upsert_deployment`, `update_image`, `add_component`, `add_component_to_deployment`, `update_component`, `refresh_deployment`, `refresh_project` |
| Verwijderen | `delete_project`, `delete_deployment`, `delete_component`, `delete_attachment` |
| Gegevens | `backup`, `restore`, `clone_database`, `clone_bucket`, `manage_database_schemas` |
| Diensten | `add_service`, `configure_service`, `configure_service_values`, `configure_attachment` |
| Slaapstand | `sleep_deployment`, `wake_deployment` |

Voorstel: **niet per taaksoort een meldingstype**, maar per groep. Vijf typen in plaats van 23, en de taaksoort staat in het gebeurtenisrecord zodat de tekst wel precies kan zijn. Zie de groepering onderaan dit document.

---

## 2. Aanvragen en goedkeuringen

**Waar**: `opi/services/catalog/approval.py` (`ApprovalSpec`, `ApprovalStatus`, `ApproverScope`, `service_use_approval()` op regel 170), `opi/services/approvals.py` (de catalogusloop), `opi/web/router_approvals.py` (de beheerpagina op `/admin/approvals`). Documentatie: `features/aanvragen-beheerpagina.md`.

Vandaag declareren **twee diensten** samen **drie goedkeuringen**:

| Dienst | Spec | Wat er goedgekeurd wordt |
|---|---|---|
| `publish-on-web` | `domain` | een eigen domeinnaam (`opi/services/catalog/publish_on_web/__init__.py:411`) |
| `publish-on-web` | `subdomain` | een subdomein onder een cluster-domein (`:420`) |
| `send-email` | `send-email` | mag dit project de dienst gebruiken (`opi/services/catalog/send_email/__init__.py:88`) |

De statussen zijn `none`, `requested`, `approved`, `denied` (`opi/services/catalog/approval.py:31`).

| gebeurtenis | bron | onderwerp | belanghebbenden | publiek | ernst | standaardkanaal |
|---|---|---|---|---|---|---|
| Een aanvraag is ingediend | `approvals.py:81` (`ensure_approval_requests`), en de dienst-eigen `_ensure_requested` in `approval.py` | dienst binnen een project | platformbeheerder (de `ApproverScope` bepaalt wie) | B | actie nodig | postvak + mail |
| Een aanvraag is goedgekeurd | `approvals.py:123` (`apply_approval_verdicts`) | idem | aanvrager, projectbeheerder | A | ter informatie | postvak + mail |
| Een aanvraag is afgewezen | `approvals.py:123` | idem | aanvrager, projectbeheerder | A | actie nodig | postvak + mail |
| Een aanvraag staat al lang open | **bestaat nog niet** (toestand ook niet: er is geen tijdstip van indienen, alleen de history-regels van oordelen) | idem | platformbeheerder | B | actie nodig | postvak |
| Een aanvraag is ingetrokken | **bestaat nog niet** (de toestand bestaat ook niet: `ApprovalStatus` kent geen `withdrawn`) | idem | platformbeheerder | B | ter informatie | postvak |

**Twee bevindingen.**

De opdracht noemt vier momenten die elk een melding verdienen: ingediend, goedgekeurd, afgewezen, ingetrokken. Er zijn er **drie**. Intrekken bestaat niet als toestand: de enum in `opi/services/catalog/approval.py:31` heeft vier waarden en `withdrawn` is er geen van. Een aanvraag intrekken zou vandaag betekenen dat je de dienst uit het projectbestand haalt, en dan is er geen goedkeuringsblok meer om iets over te melden. Als intrekken een gebeurtenis moet worden, is dat eerst een uitbreiding van de goedkeuringsmachine, en dat valt buiten dit traject.

En: **indienen is toestandsvormig, niet gebeurtenisvormig.** `_ensure_requested` is uitdrukkelijk idempotent geschreven ("lees het project zoals het staat en vul aan wat ontbreekt"), zodat een aanvraag via de API op dezelfde plek landt als een vinkje in de wizard. Dat is goed voor de goedkeuringsmachine en lastig voor gebeurtenissen: er is geen moment "hier werd de aanvraag ingediend", er is alleen "er staat nu een aanvraag". Wie er een gebeurtenis aan hangt moet de overgang zelf waarnemen (was er geen blok, is er nu wel) in plaats van te kunnen aanhaken bij een emit. Dit is het scherpste voorbeeld in dit hele document van waarom de plek waar je gebeurtenissen laat ontstaan een echte keuze is; zie deel 2, punt 1.

**Dit is verder wel een sterke kandidaat voor de vroege fasering.** Er zijn maar drie goedkeuringen, ze zijn zeldzaam (dus geen volumeprobleem), de belanghebbende is vrijwel altijd een platformbeheerder (dus weinig autorisatiewerk), en de pijn is echt: vandaag ziet niemand een aanvraag tot iemand `/admin/approvals` opent. Deel 3 zet hem als tweede schrijfweg, en zegt daar waarom niet als eerste.

---

## 3. Gezondheid van deployments

**Waar**: `opi/services/oom_watcher.py` (de waarneming), `opi/services/event_interpreter.py` (de duiding), `opi/services/deployment_state.py` (wat de diensten over een deployment weten), `opi/services/deployment_diagnostics.py` (de afwijkingen). Documentatie: `features/image-pull-backoff-detection.md`, `features/probe-kill-is-geen-crash.md`, `features/uitgeschakeld-is-niet-gezond.md`, `features/status-afwijkingen.md`, `features/argocd-render-error-surfacing.md`, `features/deployment-state-and-health.md`, `features/oom-kill-watcher.md`.

`ComponentFailure.failure_type` kent drie waarden: `oom`, `image_pull`, `crash_loop` (`oom_watcher.py:113`).

| gebeurtenis | bron | onderwerp | belanghebbenden | publiek | ernst | standaardkanaal |
|---|---|---|---|---|---|---|
| Een container is door de OOM-killer geraakt | `oom_watcher.py:199` (`check_pod_health`), detectie op `lastState.terminated.reason == "OOMKilled"` (`:293`), ingepland vanuit `:704` (`schedule_oom_check`) | component | projectbeheerder, projectlid | A en C | actie nodig | postvak + mail |
| Een image kan niet worden opgehaald | `oom_watcher.py:199`, reden uit `IMAGE_PULL_REASONS` | component | projectbeheerder, actor van de laatste image-wijziging | A en C | storing | postvak + mail |
| Een component is daarop op `replicas: 0` gezet | `oom_watcher.py:478` (`disable_components_for_image_pull`), plus `opi/handlers/project_file_handler.py` | component | projectbeheerder | A en C | storing | postvak + mail |
| Een component crasht herhaaldelijk | `oom_watcher.py:199`, `CrashLoopBackOff`; duiding in `event_interpreter.py` (`_CRASH_TITLE`) | component | projectbeheerder, projectlid | A | actie nodig | postvak |
| Een container is gedood door een falende probe | `event_interpreter.py:311` (`_probe_kill_translation`) | component | projectbeheerder | A | actie nodig | postvak |
| ArgoCD kan de manifesten niet renderen | `event_interpreter.py:562` (`interpret_argocd_errors`), `:324` (`condense_render_error`) | deployment | projectbeheerder | A en C | storing | postvak + mail |
| Een deployment wijkt af zonder dat er iets stuk is | `deployment_diagnostics.py:262` (`gather_sync_deviations`) | deployment | projectbeheerder | A | ter informatie | geen |
| Een deployment gaat van gezond naar ongezond | **bestaat nog niet** (de toestand wordt per bevraging opnieuw opgehaald in `deployment_diagnostics.py:102`, `gather_deployment_errors`; er is nergens een vorige toestand om mee te vergelijken) | deployment | projectbeheerder, projectlid | A en C | storing | postvak + mail |
| Een deployment is weer gezond | **bestaat nog niet**, om dezelfde reden | deployment | projectbeheerder | A en C | ter informatie | postvak |

**De bruikbaarste vondst van deze hele inventarisatie zit hier.** `event_interpreter.EventSeverity` (`:18`) classificeert al op `actionable` / `informational` / `noise`. Dat is precies de as die een gebeurtenissensysteem nodig heeft, hij is al gevuld voor de hele vertaaltabel van Kubernetes-redenen, en hij is al getoetst. Het nieuwe systeem hoeft die classificatie niet opnieuw te verzinnen: het kan hem overnemen. De drie waarden vertalen recht toe naar de ernst-kolom hierboven (`actionable` naar actie nodig, `informational` naar ter informatie, `noise` naar helemaal geen gebeurtenis).

**En de belangrijkste beperking.** Er is geen toestandsgeheugen. `check_pod_health` kijkt naar de pods zoals ze nu zijn; `interpret_events` leest de Kubernetes-events zoals ze nu zijn; `gather_deployment_errors` en `gather_sync_deviations` bouwen bij elk paginabezoek opnieuw een lijst op. Nergens staat wat de vorige uitslag was. "Ging over van groen naar rood" is dus geen gebeurtenis die je kunt afvangen, want niemand houdt bij wat groen was, en "sinds wanneer is dit rood" is onbeantwoordbaar. Dat maakt de gezondheidsgebeurtenissen duurder dan ze lijken: er hoort een toestandsveld bij dat de vorige uitslag onthoudt, anders krijg je bij elke ronde dezelfde melding opnieuw (en dat is precies wat de dedup uit deel 2 moet opvangen).

---

## 4. Automatisch ingrijpen door het platform

Dit is de categorie waar de wens het scherpst is: het platform verandert iets aan een deployment zonder dat de eigenaar erom vroeg, en de eigenaar hoort er niets over.

**Waar**: `opi/services/resource_tuning_service.py` plus de planner `opi/core/resource_tuning_scheduler.py:120` (`features/auto-resource-tuning.md`), `opi/services/catalog/sleep_mode/` (`features/sleep-mode.md`), `opi/jobs/service_orphan_sweep.py` plus `opi/jobs/reconciliation.py` plus `opi/core/reconciliation_scheduler.py:100` en `opi/services/marked_for_deletion_service.py` (`features/service-orphan-reconciliation.md`, `features/yaml-diff-driven-deletion.md`).

| gebeurtenis | bron | onderwerp | belanghebbenden | publiek | ernst | standaardkanaal |
|---|---|---|---|---|---|---|
| Het platform heeft het geheugen van een component bijgesteld | `resource_tuning_service.py:741` (`apply_resource_tuning`), ingepland vanuit `resource_tuning_scheduler.py:120` | component | projectbeheerder | A | ter informatie | postvak + mail |
| Het platform kon het geheugen niet verder verhogen (plafond bereikt) | `resource_tuning_service.py` via `get_max_memory_limit_mi` uit `opi/core/cluster_config.py` | component | projectbeheerder, platformbeheerder | A en B | actie nodig | postvak + mail |
| Een deployment is in slaapstand gezet | `opi/services/catalog/sleep_mode/scheduler.py:208` (`plan_sweep` plus de lus die hem toepast) | deployment | projectbeheerder, projectlid | A | ter informatie | postvak |
| Een deployment is gewekt | `opi/services/catalog/sleep_mode/scheduler.py:227`, of via de wekknop in `opi/services/catalog/sleep_mode/flow.py:98` | deployment | projectlid | A | ter informatie | geen |
| Een deployment bleef hangen in "wakker worden" en is teruggezet | idem (de `waking`-tak van de sweeper) | deployment | projectbeheerder | A | actie nodig | postvak |
| Een resource is als wees aangemerkt | `opi/jobs/service_orphan_sweep.py` (classificatie `orphan_candidate`) | dienst binnen een project | platformbeheerder, projectbeheerder | A en B | actie nodig | postvak + mail |
| Een resource lijkt wees maar is in gebruik (`in_use_anomaly`) | `opi/jobs/service_orphan_sweep.py` | dienst | platformbeheerder | B | actie nodig | ntfy |
| Een gemarkeerde resource is definitief verwijderd | `opi/jobs/reconciliation.py:219` (`_purge_marks`), via `opi/services/marked_for_deletion_service.py` | dienst | projectbeheerder | A en B | storing (onomkeerbaar) | postvak + mail |
| Een markering is teruggedraaid omdat de resource terugkwam | `opi/jobs/reconciliation.py:231` (de `unmarked`-tak in `_purge_marks`) | dienst | platformbeheerder | B | ter informatie | geen |
| De reconciliatie sloeg een ronde over omdat de projectstore leeg was | `opi/core/reconciliation_scheduler.py:96`; wordt alleen `logger.warning` | platform | platformbeheerder | B | actie nodig | ntfy |

**De automatische stemmer verdient een aparte opmerking.** Hij schrijft in het projectbestand, inclusief een `history`-blok met tijdstip, bron (`auto-tune`) en reden. Dat is het enige plekje in de hele inventarisatie waar een automatische ingreep vandaag al een duurzaam spoor achterlaat dat de gebruiker kan lezen. Het staat alleen in de YAML, er is geen actor bij, en niemand krijgt te horen dat er een regel bij kwam. Voor gebeurtenissen is dat goed nieuws: de gegevens die in de gebeurtenis moeten staan (wat, hoeveel, waarom) worden al vastgelegd. Deel 3 gebruikt precies dat feit om deze bron als eerste schrijfweg te kiezen.

**En de onomkeerbare gevallen.** "Een gemarkeerde resource is definitief verwijderd" is de enige regel in dit hele document waar de melding ACHTER de daad aankomt en er niets meer aan te doen is. Dat pleit ervoor dat dit type niet uitzetbaar is; zie deel 3, "De voorkeuren". Erger nog: de uitkomst van de reconciliatieronde is vandaag een logregel met drie tellingen (`purged=%d, unmarked=%d, errors=%d`) en de rij in `marked_for_deletion` die het voornemen droeg verdwijnt bij het opruimen, dus er blijft niets over dat zegt WAT er is weggegooid.

---

## 5. Backups en herstel

**Waar**: `opi/core/backup_scheduler.py` (de planner), `opi/core/backup_retention_sweep.py` (de opruiming), `opi/core/task_handlers_backup.py` (de uitvoering), `opi/manager/backup/`. Documentatie: `features/backup-system.md`, `features/scheduled-backups.md`, `features/backup-retention-sweep.md`.

De planner maakt taken van het type `backup` aan; de uitvoering loopt dus door de takenrij en erft alles wat daar in paragraaf 1 over staat, inclusief het uur bewaartijd.

| gebeurtenis | bron | onderwerp | belanghebbenden | publiek | ernst | standaardkanaal |
|---|---|---|---|---|---|---|
| Een geplande backup is gelukt | `backup_scheduler.py:268` via de aangemaakte `backup`-taak | project | projectbeheerder | A | ter informatie | geen |
| Een geplande backup is mislukt | `async_task_service.py:329` via de taak die `backup_scheduler.py` aanmaakte | deployment | projectbeheerder | A en C | storing | postvak + mail |
| Een geplande backup is niet eens gestart | `backup_scheduler.py:302` en `:404-405` (de tak `status == "error"`: Kopia niet bevraagbaar, dus de tick wordt overgeslagen); `:302` is een `logger.exception` in een `except Exception` en logt dus op ERROR met traceback, `:405` is een `logger.warning` | deployment | projectbeheerder, platformbeheerder | A en B | storing | postvak + mail |
| Een handmatige backup is klaar | de `backup`-taak | deployment | actor | A en C | ter informatie | postvak |
| Een herstel is klaar of mislukt | de `restore`-taak (`opi/core/task_handlers_backup.py`) | deployment | actor, projectbeheerder | A en C | storing bij mislukking | postvak + mail |
| De opruimronde heeft snapshots verwijderd | `backup_retention_sweep.py:223` en `:226` | project | projectbeheerder | A | ter informatie | geen |
| Er is al N dagen geen geslaagde backup | **bestaat nog niet** (de toestand is wel te bevragen: de planner vraagt Kopia al naar de laatste geslaagde snapshot, `backup_scheduler.py:286`) | deployment | projectbeheerder | A en B | actie nodig | postvak + mail |

**"Een geplande backup die faalt is vandaag stil" klopt, en de derde regel is erger dan de tweede.** Als de taak faalt, staat er tenminste nog een uur een `failed`-rij. Maar als de planner Kopia niet kan bevragen, slaat hij de tick over en maakt hij helemaal geen taak aan. Er is dan geen rij, geen mislukking en geen backup, alleen twee logregels: de uitzondering op ERROR met traceback (`backup_scheduler.py:302`) en het overslaan zelf op WARNING (`:405`). De ERROR-regel haalt de ntfy-drempel wel, want de logbewaker leest ERROR en CRITICAL (`log_watcher.py:16`), maar die gaat naar de platformbeheerder en niet naar de projectbeheerder die op zijn backup rekent, en er blijft niets achter dat aan de deployment hangt.

De laatste regel ("al N dagen geen geslaagde backup") is de melding die dit gat echt dicht, want hij hangt niet aan een gebeurtenis maar aan het UITBLIJVEN ervan. Dat is een ander soort melding en het is de moeite waard om hem apart te noemen: hij vraagt een periodieke controle, geen haak in een codepad. Deel 3 wijst hem toe aan de metriekhelft (Alertmanager) en niet aan de gebeurtenissenlog.

---

## 6. Leden, uitnodigingen en toegang

**Waar**: `opi/manager/invite_manager.py`, `opi/api/invite_routes.py`, `opi/services/catalog/invite/`, `opi/services/project_authorization.py`. Documentatie: `features/invite-system.md`, `features/invites.md`, `features/zad-external-user-support.md`.

Belangrijk om te weten hoe dit werkt: een uitnodiging is een **gedeelde link met een code**, geen persoonlijke uitnodiging. ZAD kent de persoon niet voor hij hem inwisselt (`opi/services/catalog/invite/__init__.py`: "De code IS de uitnodiging"). Er is dus geen "jij bent uitgenodigd"-melding mogelijk, want er is geen adres om hem heen te sturen.

| gebeurtenis | bron | onderwerp | belanghebbenden | publiek | ernst | standaardkanaal |
|---|---|---|---|---|---|---|
| Er is een uitnodiging aangemaakt | `opi/api/invite_routes.py`; wordt `logger.info` | project | projectbeheerder | A | ter informatie | postvak |
| Iemand heeft een uitnodiging ingewisseld via SSO | `invite_manager.py:262` (`complete_sso_invite`), terugkomstroute `opi/api/invite_routes.py:604` | project | projectbeheerder | A | ter informatie | postvak |
| Iemand heeft een uitnodiging ingewisseld met een lokaal account | `invite_manager.py:355` (`complete_local_invite`), registratieroute `opi/api/invite_routes.py:721` | project | projectbeheerder | A | ter informatie | postvak |
| Een inwisseling is geweigerd (verkeerd domein, verkeerde methode) | `invite_manager.py:73` (`validate_email_domain`), `:101` (`validate_auth_method`) | project | projectbeheerder | A en B | ter informatie | geen |
| Een uitnodigingscode is ongeldig of verlopen | `invite_manager.py:128` (`get_valid_invite`) | project | projectbeheerder | A | ter informatie | geen |
| Iemand is als lid aan een project toegevoegd | **bestaat nog niet als gebeurtenis**: dit is een wijziging aan de `users:`-lijst in het projectbestand via `opi/forms/editables/fields/team.py:22` (`USERS_SEQUENCE_EDITABLE`) en `opi/web/router_detail_edit.py` (`save_and_commit_project`), en landt als git-commit onder de systeemidentiteit | project + gebruiker | de toegevoegde persoon, projectbeheerder | A | ter informatie | postvak + mail |
| Iemands rol in een project is gewijzigd | **bestaat nog niet als gebeurtenis**, zelfde weg | project + gebruiker | de betrokkene, projectbeheerder | A | ter informatie | postvak |
| Iemand is uit een project verwijderd | **bestaat nog niet als gebeurtenis**, zelfde weg | project + gebruiker | de betrokkene, projectbeheerder | A | actie nodig | postvak + mail |
| Een projectrol is aan een realm-gebruiker toegekend | `invite_manager.py:187` (`assign_invite_permissions`), `:153` (`_assign_client_roles`) | gebruiker | platformbeheerder | B | ter informatie | geen |

**De drie ledenwijzigingen zijn het lastigste geval in deze hele inventarisatie**, en het is de moeite waard te zien waarom. Ze zijn geen aanroep in een codepad maar een verschil tussen twee versies van een YAML-lijst. Wie er een gebeurtenis aan wil hangen moet de oude en de nieuwe `users:` vergelijken op het moment van opslaan. Dat kan (de opslagroute heeft beide in handen), maar het is een ander soort werk dan een `emit()` neerzetten, en het geldt voor elk veld in het projectbestand dat ooit een gebeurtenis moet worden. Zie deel 2, punt 1, de variant "de opslagweg vergelijkt".

En let op de asymmetrie: "je bent lid geworden" gaat naar iemand die op dat moment nog geen lid was, en "je bent verwijderd" naar iemand die het niet meer is. Beide vallen buiten de gewone autorisatieregel "je ziet gebeurtenissen van je eigen projecten". Dat is precies de vraag die in deel 2 bij richting C beantwoord moet worden, en de reden dat het antwoord niet "we bevragen op het moment van kijken" kan zijn. **En let er ook op dat deze negen rijen allemaal de langere bewaartermijn uit deel 2, punt 6 dragen.** Ze gaan alle negen over een recht of over een toegangspoging: een uitnodiging is een uitgedeeld recht, een inwisseling is het opnemen ervan, een weigering is een afgeslagen poging, en de drie ledenwijzigingen en de projectrol zijn het recht zelf. Dat ze in type 8 vallen en de geweigerde API-sleutel in type 12 doet daarbij niet ter zake: die termijn hangt aan de SOORT en niet aan het type, en deel 2 geeft het criterium en de gesloten lijst. **En let ook op de laatste rij van de tabel, want die staat er anders bij dan zijn vijf buren uit dezelfde uitnodigingsketen (`:270` tot en met `:274`, alle vijf met onderwerp `project`).** "Een projectrol is aan een realm-gebruiker toegekend" (`:278`) heeft als onderwerp `gebruiker`, maar hij DRAAGT wel degelijk een project: hij ontstaat op `realm_name`, en dat is bij ZAD de realm van een project. Dat staat er letterlijk, in de docstring van allebei de aanroepers: "The project's Keycloak realm name" (`invite_manager.py:281` en `:375`, in `complete_sso_invite` en `complete_local_invite`, die daarnaast een eigen parameter `project_name` hebben). Dat verschil doet ertoe in deel 2, punt 4: de bereikgrendel daar telt op de projectSCOPE en niet op de kolom `onderwerp`, en deze rij is een van de zes waarop hij bijt. **En daarom staat er nu `platformbeheerder` in de kolom belanghebbenden en niet `projectbeheerder`; dat is een correctie op het bronstuk, met reden.** Het bronstuk (`plans/meldingen-inventarisatie.md`, inmiddels verwijderd) schreef hier `projectbeheerder` in een tabel die nog geen kolom `publiek` had, dus daar botste niets. Met die kolom erbij botst het wel: deze rij draagt publiek `B` en niets anders, en met de grendel uit deel 2 zou de enige genoemde belanghebbende hem dus nooit te zien krijgen. Het criterium daarachter is niet "de belanghebbendenkolom noemt ALLEEN een projectrol" maar "elke belanghebbende die de kolom noemt moet de rij langs enige weg kunnen krijgen"; die eerste, smallere formulering liet `:141` staan, want daar stond de platformbeheerder naast de actor. Op het scherpere criterium gedraaid over de zevenentwintig rijen in deze catalogus met publiek uitsluitend `B` (van de negenenzeventig in totaal, met een `idem`-cel opgelost naar de rij erboven) vielen er twee door: deze en `:141`. Beide staan nu goed, deze als correctie op het bronstuk en `:141` als correctie binnen RC-163 op een waarde die RC-163 zelf had toegekend, en de vijfentwintig andere noemen niemand die de rij niet kan krijgen. Van de twee kanten is `B` de kant die klopt: dit is de Keycloak-kant van een inwisseling en niet de inwisseling zelf. Die staat op `:271` en `:272`, draagt publiek `A` en gaat naar het postvak, en dat is wat de projectbeheerder op hetzelfde moment te zien krijgt. Deel 2, punt 6 bouwt de onderbouwing van de langere bewaartermijn juist op deze soort ("wie een beheerdersessie overneemt en zichzelf een projectrol toekent"), en dat is incidentonderzoek en dus publiek `B`.

---

## 7. Beheerders- en platformgebeurtenissen

**Waar**: `opi/services/user_admin_service.py` (`features/user-admin-crud.md`), `opi/services/user_service.py` (de platform-adminlijst), `opi/middleware/authorization.py`, `opi/api/endpoint_util.py`, `opi/api/user_token_auth.py`, `opi/core/startup.py`, `opi/core/caa_reconciler.py`, `opi/core/no_mail_reconciler.py`, `opi/core/federation_service.py`, `opi/services/project_store.py`, en voor de clusterbrede zaken grotendeels: buiten OPI.

| gebeurtenis | bron | onderwerp | belanghebbenden | publiek | ernst | standaardkanaal |
|---|---|---|---|---|---|---|
| Een gebruiker is aangemaakt in het platformregister | `user_admin_service.py:41` (`create_user`) | gebruiker | platformbeheerder | B | ter informatie | postvak |
| Een gebruiker is gewijzigd | `user_admin_service.py:51` (`update_user`) | gebruiker | platformbeheerder | B | ter informatie | geen |
| Een gebruiker is verwijderd | `user_admin_service.py:65` (`delete_user`) | gebruiker | platformbeheerder | B | actie nodig | postvak |
| Een gebruiker die niet op de allowlist staat probeerde binnen te komen | `opi/middleware/authorization.py:117`; wordt `logger.warning`, daarna een 302 naar `/permission-denied` | platform | platformbeheerder | B | actie nodig | postvak + ntfy |
| Een API-sleutel of bearer-token is geweigerd | `opi/api/endpoint_util.py:52`, `:69`, `:135`, `:176` en `opi/api/user_token_auth.py:252`; wordt `logger.warning`, en draagt de routenaam maar **niet** het IP-adres en **niet** het project | platform | platformbeheerder | B | actie nodig | postvak + ntfy |
| Inloggen en uitloggen | Keycloak, niet OPI: de realm-configuraties zetten `eventsEnabled`, `eventsExpiration: 7776000` (90 dagen) en `adminEventsEnabled` aan (`opi/configs/keycloak/bootstrap.yaml:19-26`, `opi/configs/keycloak/sso-support.yaml:44-51`, en vier andere), sinds commit `b503436e` van 20 juli 2026 | platform | platformbeheerder | B | ter informatie | geen |
| Een opstartfase van OPI is mislukt en wordt herprobeerd | `opi/core/startup.py:663` (`_startup_retry_loop`); logregels plus de statuspagina uit `opi/core/readiness.py` | platform | platformbeheerder | B | actie nodig | ntfy |
| De CAA-records of de no-mail-records op onze DNS-zones weken af | `opi/core/caa_reconciler.py:69`, `opi/core/no_mail_reconciler.py:113`-`:155`; wordt `logger.warning`. Zie `features/caa-records.md` en `features/no-mail-dns-records.md` | platform | platformbeheerder | B | actie nodig | ntfy |
| Een out-of-band bewerking van `zad-projects` is opgepikt door de trage reconcile-poll | `opi/services/project_store.py:1343`; niets bij succes, alleen een `logger.error` bij een fout | platform | platformbeheerder | B | ter informatie | postvak |
| Een federatietaak is doorgestuurd naar een ander cluster | `opi/core/federation_service.py:60` en `:90`; zie `features/federation-routing.md` | project | platformbeheerder | B | ter informatie | geen |
| Een subdomein is geclaimd of vrijgegeven | `opi/services/persistence/subdomain_registry.py:211`, `:301`, `:320`, `:464` (schrijft naar de logger `opi.audit.subdomain`) | platform | platformbeheerder | B | ter informatie | geen |
| Iemand is platformbeheerder geworden of afgevoerd | **bestaat nog niet**: de allowlist komt uit de configuratie (`UserService.is_platform_admin`), niet uit een handeling in de applicatie | platform | platformbeheerders | B | actie nodig | postvak + mail |
| Er is een nieuwe release van het platform | **bestaat nog niet**: er is een `/version`-endpoint met de bouwgegevens, maar niets dat een wijziging daarvan waarneemt | platform | iedereen | A en B | ter informatie | postvak |
| Onderhoud is gepland | **bestaat nog niet**, in geen enkele vorm | platform | iedereen | A en B | actie nodig | postvak + mail |
| De beveiligingsscan heeft bevindingen | **bestaat nog niet in OPI**: de scan draait in GitHub Actions (`.github/workflows/security.yml`, zie `features/security-scanning-pipeline.md`) en meldt in GitHub, niet in ZAD | platform | platformbeheerder | B | actie nodig | (zie hieronder) |
| Een gebruikte image is verouderd | **bestaat nog niet als lopend proces**: `features/image-version-audit.md` is een handmatig onderzoek van februari 2026, geen controle die draait | platform | platformbeheerder | B | actie nodig | (zie hieronder) |
| Een certificaat verloopt binnenkort | **bestaat nog niet**: het contactadres in de clusterconfiguratie is dat van de ACME-account, dus Let's Encrypt mailt rechtstreeks en ZAD weet er niets van | platform | platformbeheerder | B | actie nodig | (zie hieronder) |

**De vier beveiligingsregels bovenaan (allowlist, sleutel, token, inloggen) zijn het spoor dat je bij een incident maanden later terug wilt lezen.** Ze zijn vandaag logregels en verder niets, en de Keycloak-auditevents die het inloggedeelte dekken staan waarschijnlijk niet aan op realms die van voor 20 juli 2026 dateren; zie de twee correcties verderop. Deze regels krijgen daarom in deel 2 een eigen, langere bewaartermijn dan de gewone beheergebeurtenissen, **en ze zijn niet de enige in deze paragraaf die hem krijgt**: de drie accountregels erboven (aangemaakt, gewijzigd, verwijderd) en "iemand is platformbeheerder geworden of afgevoerd" krijgen hem net zo goed. De reden staat in deel 2 en is de moeite hier te herhalen: een aanval laat meestal geen weigering achter, want wie een beheerdersessie overneemt en zichzelf rechten toekent krijgt overal een 200, en dan is de geslaagde rechten- of accountwijziging het enige spoor dat er is. Die termijn hangt aan de SOORT en niet aan het type waarin ze vallen: de zestien andere rijen van type 12 zijn gewoon beheer en volgen de gewone klok, en omgekeerd draagt heel paragraaf 6 hierboven (type 8) dezelfde langere termijn.

**Over de laatste drie regels.** Dit zijn geen gebeurtenissen die ZAD kan afvangen, want het zijn geen gebeurtenissen in ZAD. Ze horen in de inventarisatie omdat ze in de opdracht staan en omdat het antwoord "buiten de deur" een echt antwoord is. Wie ze binnen ZAD wil hebben, bouwt eerst iets dat kijkt (een GitHub-webhook die de scanuitslag binnenhaalt, een periodieke vergelijking van draaiende images tegen upstream, een controle op de vervaldatum van de certificaten die het cluster serveert). Dat is per stuk een eigen opdracht, en het is verstandig dat ze niet in de eerste fasen zitten.

Wat wel in het plan hoort: **de gebeurtenissenmachine moet gebeurtenissen van buiten kunnen aannemen.** Als een GitHub Action een bevinding kan POSTen naar een intern endpoint, is de scanmelding een kwestie van dat endpoint en niet van een nieuw systeem. Dat is een goedkoop ontwerpbesluit dat nu genomen moet worden en later duur is om alsnog in te bouwen.

---

## 8. Kortlopende workloads

**Waar**: `opi/services/runs_service.py`, `opi/services/persistence/runs.py`, `opi/core/runs_schema.py`, `opi/core/db_console_reaper.py`, `opi/manager/db_console_manager.py`, gestart via `opi/manager/run_support.py:56`.

`RunKind` kent `db-console` en `job` (gepland, `runs_service.py:25`). `RunStatus` kent `starting`, `running`, `succeeded`, `failed`, `stopped`, `expired` (`:32`).

| gebeurtenis | bron | onderwerp | belanghebbenden | publiek | ernst | standaardkanaal |
|---|---|---|---|---|---|---|
| Een databaseconsole is gestart | `runs_service.py:46` (`create_run`) | deployment | projectbeheerder | A en B | ter informatie | postvak |
| Een console is beeindigd door de gebruiker | `runs_service.py:124` (`mark_ended`, status `stopped`) | deployment | actor | A | ter informatie | geen |
| Een console is verlopen en opgeruimd | `db_console_reaper.py:157`, `:194`, `:229`, via `mark_ended` met status `expired` | deployment | actor | A | ter informatie | postvak |
| Een run is mislukt | `runs_service.py:124` met status `failed`, veld `error_message` | deployment | actor, projectbeheerder | A en C | actie nodig | postvak |
| Een ad-hoc job is klaar | **bestaat nog niet**: `RunKind.JOB` staat in de enum met het commentaar "planned: ad-hoc pod running an image + command" | deployment | actor | A en C | ter informatie | postvak |

**Een console starten is een beheerdersgebeurtenis vermomd als gebruikersgebeurtenis.** Iemand opent een directe verbinding met de productiedatabase van een project. De `runs`-tabel legt `started_by`, `started_at` en `ended_by` vast, en de tabel wordt niet opgeruimd (anders dan taken), dus het spoor blijft. Dat is een goede reden om het als gebeurtenis aan de andere projectbeheerders te tonen: niet omdat er iets mis is, maar omdat het het soort handeling is waar collega's van horen te weten. Dit is meteen het duidelijkste voorbeeld van een gebeurtenis die tegelijk in het meldingssysteem EN in een audittrail thuishoort; zie deel 2, punt 5. Van alle regels in dit document is dit de enige waarvan je kunt zeggen dat het vandaag al goed geregeld is. **En let op `:328`, want daar wijkt de kolom `publiek` af van het enige bronstuk dat er iets over zei.** De gebeurtenissenset legde "Een databaseconsole of jobbundel is verlopen en opgeruimd", met precies dezelfde ankers (`db_console_reaper.py:157`, `:194`, `:229`), in groep B (`features/futures/gebeurtenissen-inventarisatie.md:86` op `e98a7811`); hier staat `A`. Dat is een correctie op het bronstuk, met reden, en het is de enige bronregel in deze catalogus die op ankeroverlap aan een rij te binden is en niet overeenkomt; de telling daarvan staat in de Herkomst-alinea (`:13`). De reden is het criterium uit regel `:282` van dit document: elke belanghebbende die de kolom noemt moet de rij langs enige weg kunnen krijgen. De belanghebbende is hier de actor, de projectgebruiker van wie de sessie wordt beeindigd, en met publiek uitsluitend `B` zou die hem onder de bereikgrendel van deel 2, punt 4 nooit te zien krijgen, precies zoals op `:141` en `:278`. Zijn buurrij `:327` beschrijft dezelfde beeindiging door een andere oorzaak en draagt om dezelfde reden `A`. Wat het bronstuk in groep B zag zit in deze catalogus op de eerste rij van deze tabel (`:326`, publiek `A en B`): dat iemand een directe verbinding met de productiedatabase opent is wat de platformbeheerder wil weten, en het opruimen daarna is het sluiten van een sessie waarvan het openen op die rij al wordt gemeld.

---

## 9. Wat er al een kanaal heeft: de logbewaker en ntfy

**Waar**: `opi/services/log_watcher.py` (de pijplijn), `opi/core/logwatcher_scheduler.py` (de planner in de applicatie), `operations-manager/python/scripts/log_watch/watch.py` (de losse CLI met Claude-triage). Documentatie: `features/log-watcher.md`.

Wat het doet, in vier stappen (`log_watcher.py:16`): het bevraagt Loki via de Grafana-datasource-API op ERROR- en CRITICAL-regels uit de OPI-container over een venster van 35 minuten, gooit alles weg dat op de negeerlijst staat (`opi/services/log_watch_ignore_patterns.txt`, 91 regels), ontdubbelt de rest tegen een toestand (standaard 6 uur, `LOGWATCHER_DEDUP_HOURS`, ook zichtbaar als `dedup_hours: float = 6.0` op `log_watcher.py:74`), en POST wat overblijft naar ntfy (`log_watcher.py:327`, `send_ntfy`).

| gebeurtenis | bron | onderwerp | belanghebbenden | publiek | ernst | standaardkanaal |
|---|---|---|---|---|---|---|
| OPI zelf logt een ERROR of CRITICAL | overal in de code; opgepikt uit Loki door `log_watcher.py` | platform | platformbeheerder | B | actie nodig | ntfy |

| Eigenschap | Waarde | Waar |
|---|---|---|
| Standaard aan | nee, `LOGWATCHER_ENABLED = False` | `opi/core/config.py:441` |
| Interval | 1800 seconden | `opi/core/config.py:442` |
| Server | `https://ntfy.sh` | `opi/core/config.py:444` |
| Ontdubbelvenster | 6 uur | `opi/core/config.py:448` |
| Onderwerp | een geheim, onraadbaar topic ("treat like a password") | `opi/core/config.py:443` |
| Toestand | een dict in het geheugen van de planner, verdwijnt bij een herstart | `logwatcher_scheduler.py:32` |

**Hoe dit zich tot het nieuwe verhoudt: naast elkaar, niet erin op.** Drie redenen, en ze zijn alle drie hard.

1. **Het publiek is anders.** De logbewaker meldt fouten uit het OPI-logboek: stacktraces, uitzonderingen, dingen die van ons zijn en niet van de klant. Een projectbeheerder heeft er niets aan en zou er niets van moeten zien. Dit is publiek B, niet A.
2. **De bron is anders.** Alle gebeurtenissen in dit document ontstaan in de code van OPI zelf, op een moment dat OPI kent. De logbewaker ontstaat in Loki, achteraf, uit tekst. Dat is een fundamenteel andere pijplijn (bevragen, ontdubbelen op een genormaliseerde tekstsleutel) die niets deelt met "leg een record aan als dit gebeurt".
3. **Het kanaal is bewust laagdrempelig.** ntfy op een geheim topic vraagt geen account, geen koppeling en geen aflevergarantie. Precies goed voor "de beheerder krijgt een piep", en precies verkeerd als vervanger voor een postvak met leesstatus of een tijdlijn per project.

**Wat het nieuwe systeem er wel van moet erven, en dat is niet niets:**

- **Het ontdubbelmodel.** `signature()` (`log_watcher.py:312`) normaliseert een melding tot een stabiele sleutel door tijdstempels, IP-adressen, gekoppelde identifiers en losse getallen weg te strippen, tot maximaal 120 tekens. Daar bovenop een venster in uren. Dat is precies het model dat "twintig herstarts is een melding" oplost, het is uitgeschreven, en het is in productie beproefd.
- **En de fout die erin zit.** De toestand van de ingebouwde planner is een dict in het geheugen, dus na een herstart begint de ontdubbeling opnieuw. Het commentaar noemt dat "at worst repeats one alert", en voor ntfy is dat waar. Voor een postvak of een mail is het dat niet: dan krijgt iedereen na elke uitrol dezelfde meldingen opnieuw. **De ontdubbelstaat van het nieuwe systeem hoort in Postgres, niet in het geheugen.**
- **En de vier andere drempels die hij nodig bleek te hebben.** Een begrenzing op tien regels per bericht (`MAX_BODY_LINES = 10`, `:43`), een uitsluiting van zijn eigen logregels om te voorkomen dat hij op zichzelf alarmeert (`SELF_LOG_EXCLUDE`, `:48`), een regex die alleen echte Python-logrecords als aparte melding telt omdat Loki elke stackframe als eigen regel opslaat (`_LOG_RECORD_RE`, `:55`), en de negeerlijst van 91 regels. Dat is vijf lagen ruisonderdrukking op een systeem dat een ding doet; een gebeurtenissensysteem met vijftien soorten heeft ze allemaal nodig. Zie deel 2, punt 3.

**Ook meenemen**: ntfy staat standaard op `ntfy.sh`, dus buiten onze deur. Op productie kan dat: de namespace `rig-prd-operations` draagt `egress.projectcalico.org/egressGatewayPolicy: "internet"` (`bootstrap/rig-system/kustomize/overlays/odcn-production/namespace.yaml:7`) en het netwerkbeleid van OPI laat uitgaand verkeer op 443 naar elke bestemming toe (`bootstrap/rig-system/kustomize/operations-manager/overlays/odcn-production/network-policy.yaml`). Dat is relevant voor deel 3, want het bepaalt ook wat er met een webhook en met Mattermost kan.

---

## 10. Wat er verder uit de code kwam

De acht bronnen uit de opdracht zijn een startpunt en geen afbakening. Dit kwam er nog uit:

| gebeurtenis | bron | onderwerp | belanghebbenden | publiek | ernst | standaardkanaal |
|---|---|---|---|---|---|---|
| Mijn projectbestand is afgekeurd door de schemavalidatie en is niet verwerkt | `opi/core/git_monitor.py:150`; wordt een `logger.error` en verder niets. Veroorzaker: een mens, of buiten via een directe push | project | projectbeheerder, platformbeheerder | A en B | storing | postvak + mail |
| Een mail die mijn project verstuurde is niet bezorgd | de relay (Stalwart), buiten OPI. De DSN wordt aan een adres gericht dat de upstream ook weigert, waarna de relay "discarding message after double bounce" noteert en het bericht weggooit; de relaylog bewaart drie uur (`TODO.md` punt 26, uitgewerkt in `plans/mail-vervolgpunten.md`) | project | projectbeheerder | A | storing | postvak + mail |
| Een project heeft toegang tot een ander project gekregen of verloren | `opi/services/catalog/cross_domain_access/` (dienst `cross-domain-access`) | project (twee projecten tegelijk) | beheerders van BEIDE projecten | A en B | actie nodig | postvak + mail |
| Een bijlage is toegevoegd, gewijzigd of verwijderd | de taken `configure_attachment` en `delete_attachment`; dienst `opi/services/catalog/attachments/` | deployment | projectbeheerder | A | ter informatie | postvak |
| Een geheim of sleutel is geroteerd | **bestaat nog niet als gebeurtenis**: rotatie gebeurt bij het opnieuw verwerken van een project | project | projectbeheerder | A en B | ter informatie | postvak |
| Een dienst is aan een project toegevoegd of eruit gehaald | de taken `add_service` en de dienstverwijdering (`handle_service_removal` per dienst), `opi/core/task_handlers_components.py` | project | projectbeheerder | A en C | ter informatie | postvak |
| De bootstrap in git wijkt af van wat er draait | **bestaat nog niet**: `bootstrap/rig-system/kustomize/overlays/odcn-production` wordt met de hand toegepast via `task bootstrap-argo-system`. Punt 11 van `plans/mail-vervolgpunten.md` beschrijft het probleem en stelt een detectie voor; `TODO.md` punt 27 heeft het op 21 augustus 2026 bewezen | platform | platformbeheerder | B | storing | ntfy |
| Een clonebewerking heeft een nieuwe generatie gemaakt | `opi/manager/revision_manager.py`, via `opi/services/catalog/shared/revisions.py` | deployment | projectbeheerder | A | ter informatie | geen |
| Een gedeeld cluster raakt vol, of een subprocess vreet geheugen | `opi/core/metrics.py` (`OPICollector`); gaat naar `/metrics`, zonder alerteringsregels | platform | platformbeheerder | B | actie nodig | (hoort bij de metriekhelft, zie deel 3) |

**`cross-domain-access` verdient de aandacht die het niet krijgt in de opdracht.** Het is de enige gebeurtenis in de hele inventarisatie waarbij de belanghebbende in een ANDER project zit dan waar de gebeurtenis ontstaat. Dat breekt de aanname "je ziet gebeurtenissen van je eigen projecten" die alle andere regels stilzwijgend maken, en het is precies het soort geval dat een bevragingsmodel (richting B in deel 2) lastig maakt: de autorisatieregel is niet "is deze persoon lid van het project van de gebeurtenis" maar "is deze persoon lid van een van de twee projecten die de gebeurtenis noemt".

---
## Toestand die wel wordt bepaald maar niet als gebeurtenis bestaat

Dit is de tegenhanger van "bestaat nog niet, en de toestand ook niet": hier IS er een toestand, hij wordt zelfs regelmatig uitgerekend, maar er blijft niets van over.

- **Gezondheid en afwijkingen van een deployment.** `gather_deployment_errors` en `gather_sync_deviations` in `opi/services/deployment_diagnostics.py` bouwen bij elk paginabezoek opnieuw een lijst `errors` en `deviations` op uit de ArgoCD-status en het cluster. Er wordt niets bewaard, dus "sinds wanneer is dit rood" is onbeantwoordbaar, en "het was gisteren ook al rood" is niet vast te stellen. Zie `features/deployment-state-and-health.md` en `features/status-afwijkingen.md`.
- **Toestand die een dienst zelf bijdraagt.** `DeploymentStateFact` (`opi/services/catalog/base.py`, geproduceerd door `opi/services/catalog/deployment_health/__init__.py:60` en `opi/services/catalog/sleep_mode/__init__.py:75`) is een antwoord op de vraag "wat weet jij over deze deployment", berekend uit het projectbestand. Ook dat is een momentopname.
- **ArgoCD sync- en healthovergangen.** OPI leest ze (`opi/core/simple_background.py`, `features/argocd-sync-wait.md`), maar bewaart alleen de eindstand in een taak. De overgang zelf, en dus de duur van een storing, bestaat nergens.
- **Renderfouten van ArgoCD.** `features/argocd-render-error-surfacing.md` beschrijft hoe ze zichtbaar worden gemaakt; ze worden niet bewaard.
- **Het onderscheid tussen een probe-kill en een echte crash.** `features/probe-kill-is-geen-crash.md` beschrijft de logica; de uitkomst wordt getoond, niet bewaard.

Dit is de reden dat de gezondheidsregels in paragraaf 3 duurder zijn dan ze lijken, en het is de openstaande beslissing die deel 3 achteraan zet (beslissing 14 van de vijftien): gezondheidsovergangen vastleggen is het enige dat "sinds wanneer" echt beantwoordt, en tegelijk de grootste bron van ruis.

---

## Achtergrondprocessen die alleen loggen

Dit is de volledige lijst uit de lifespan van `opi/server.py`, in startvolgorde, aangevuld met de lussen die daarbuiten beginnen. Elk van deze schrijft zijn uitkomst uitsluitend naar de logger, tenzij anders vermeld. Drieentwintig regels, want de genummerde stappen tellen de subprocessen 5a tot en met 5d mee.

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
| 13 | ReconciliationScheduler, de nachtelijke opruiming | `opi/server.py:247` | `opi/core/reconciliation_scheduler.py`, `opi/jobs/reconciliation.py` | Een logregel met tellingen |
| 14 | FederationService, alleen in master-modus | `opi/server.py:280` | `opi/core/federation_service.py` | Taken op het doelcluster plus logger |
| 15 | DbConsoleReaper, de run-reaper | `opi/server.py:293` | `opi/core/db_console_reaper.py` | Tabel `runs` plus logger |
| 16 | LogwatcherScheduler | `opi/server.py:304` | `opi/core/logwatcher_scheduler.py`, `opi/services/log_watcher.py` | ntfy plus logger |
| 17 | SleepModeScheduler | `opi/server.py:315` | `opi/services/catalog/sleep_mode/scheduler.py` | `SLEEP_DEPLOYMENT`-taken, het projectbestand, plus logger |
| 18 | OOM- en gezondheidswatcher, fire-and-forget na elke uitrol | `opi/services/oom_watcher.py:704` | `opi/services/oom_watcher.py` | Taken plus logger; zie `features/oom-kill-watcher.md` |
| 19 | Verbindingsherstel van de kubectl-connector | `opi/connectors/kubectl.py:120` en `:168` | `opi/connectors/kubectl.py` | Logger |

Ten opzichte van het startpunt van de opdracht zijn de nummers 1, 2, 3, 5c, 5d, 7, 9, 11, 14, 15 en 19 toegevoegd, en is nummer 4 gepreciseerd (aanwezig maar uit).

### Twee correcties op het startpunt

**De git-monitor slikt schemafouten niet meer stil.** Het startpunt zegt dat validatiefouten daar stil worden geslikt. Dat was zo en is het niet meer: `opi/core/git_monitor.py:144-150` vangt `ProjectSchemaError` en schrijft `logger.error("Projectbestand ... afgekeurd door schemavalidatie en NIET verwerkt: ...")`. Het commentaar erboven noemt de aanleiding: "the old silence meant 22 production files were being skipped here with nobody able to see it". Een logregel is nog steeds geen gebeurtenis, en de gebruiker van dat project ziet er nog steeds niets van, maar de stilte is weg.

**De Keycloak-auditevents zijn wel gecommit.** Het startpunt zegt dat ze niet gecommit zijn. Ze staan in zes realm-configuraties, toegevoegd in commit `b503436e` van 20 juli 2026, met de post-mortem als expliciete aanleiding in het commentaar. Wat wel klopt is dat ze op bestaande productierealms waarschijnlijk niet aan staan, en de precieze oorzaak staat in `plans/technische-review-bio-en-nora-bevindingen.md`: `opi/connectors/keycloak.py:203-210` herhaalt de instellingen alleen op een 409 uit `create_realm()`, en de reconcile-weg voor een reeds bestaand projectrealm roept `create_realm()` helemaal niet aan. Elk realm dat voor 20 juli 2026 is aangemaakt heeft de instelling dus nooit met terugwerkende kracht gekregen. Of dat op productie inderdaad zo is, is **niet geverifieerd**: daarvoor is een blik op het draaiende cluster nodig.

---

## Wat er aan meld- en exportinfrastructuur ligt

| Voorziening | Toestand | Bewijs |
|---|---|---|
| **ntfy** | In gebruik, een publiek: het platformteam | `opi/core/logwatcher_scheduler.py`, `opi/services/log_watcher.py:327` (`send_ntfy`) |
| **De eigen mailrelay (Stalwart)** | Draait, en ZAD heeft er een eigen account op | `opi/connectors/mail.py`, `opi/manager/mail_manager.py:248` (`ensure_platform_account`), aangeroepen vanuit `opi/core/startup.py:438` |
| **Mail versturen vanuit OPI** | Bestaat niet | Er is geen enkele aanroep van `smtplib`, `aiosmtplib` of een SMTP-client in `opi/`; de relay wordt alleen via zijn beheer-API benaderd. De docstring van `ensure_platform_account` zegt zelf dat dit account is "what unblocks password reset and invite mail" |
| **Naar buiten mailen** | Werkt niet | Gemeten op 21 augustus 2026: een adres binnen `rijksoverheid.nl` krijgt `250 ok`, een extern adres een `550 #5.1.0 Address rejected` bij `rmrmail.rijksweb.nl` (`TODO.md` punt 26, uitgewerkt in `plans/mail-vervolgpunten.md` punt 8) |
| **OpenTelemetry** | Volledig aanwezig, uit | Negen `opentelemetry-*`-afhankelijkheden in `operations-manager/python/pyproject.toml:66-74` met instrumentatie voor FastAPI, httpx, aiohttp, asyncpg, SQLAlchemy en logging; `opi/core/tracing.py`; `OTEL_ENABLED: bool = False` |
| **Een OTLP-ontvanger** | Bestaat niet in deze repo | `OTEL_EXPORTER_OTLP_ENDPOINT` wijst naar `http://jaeger.rig-system:4317` (`opi/core/config.py:274`), maar er staat geen Jaeger in `infrastructure/bootstrap/infrastructure/` |
| **De exporter zelf** | Alleen traces | `opi/core/tracing.py` importeert uitsluitend `OTLPSpanExporter`; er is geen log- of metriekexporter |
| **Prometheus** | Draait, exporteert alleen procesinterne toestand van OPI | `opi/core/metrics.py`: uitsluitend `GaugeMetricFamily`, geen enkele `Counter` voor een domeingebeurtenis |
| **Alertmanager en alerteringsregels** | Bestaan niet | `infrastructure/bootstrap/infrastructure/prometheus/controller/base/configmap.yaml` heeft alleen `scrape_configs`, geen `rule_files` en geen `alerting`; er is geen bestand in `infrastructure/` of `bootstrap/` dat het woord alertmanager noemt |
| **Loki, Grafana en Mimir** | Buiten deze repo, wel in gebruik | `GRAFANA_URL` wijst naar `grafana-service.rig-system.svc.cluster.local:3000` (`opi/core/config.py:434`), met `GRAFANA_TOKEN` en `GRAFANA_DATASOURCE_UID` ernaast (`:435-437`); de productie-configmap zet `GRAFANA_DATASOURCE_UID=mimir-prd` en `GRAFANA_BILLING_DATASOURCE_UID=mimir-billing` (`bootstrap/rig-system/kustomize/operations-manager/overlays/odcn-production/configmap.yaml:47-48`). Er staat geen Loki-, Grafana- of Mimir-component in `infrastructure/`: de stack wordt geleverd, niet beheerd |
| **Kubernetes-events** | Per taak opgehaald | `opi/connectors/kubectl.py:969` (`get_namespace_events`), weggeschreven in de `events`-kolom van `async_tasks` via `opi/core/persistent_task_progress.py:137` |
| **De dienst-eventregistry** | Een dispatchmechanisme, geen geschiedenis | `opi/services/services_enums.py:149` (`ActionEvent`, twee waarden op `:176-177`) en `:184` (`UIEvent`, drie waarden op `:201-203`); zie `features/service-event-hooks.md` |

Dat de enige bestaande meldketen (log watcher naar ntfy) afhangt van een Loki en een Grafana die niet in deze repo staan, is op zichzelf een risico: de keten is niet reproduceerbaar op te bouwen vanuit dit versiebeheer.

---

## Wat een agent of script op de API vandaag kan zien

Dit is geen tweede catalogus maar het mechanisme voor publiek C. De agent is vandaag structureel het slechtst bediend, en om een reden die niet in de tabellen zichtbaar is: hij is niet te onderscheiden van elke andere houder van dezelfde sleutel. Een taak die via de API is gestart krijgt `created_by = "API"` (`opi/core/task_helpers.py:63`), letterlijk die string, want de sleutel identificeert het project en niet de handelende partij.

| Wat hij wil weten | Waar hij het vandaag vandaan haalt | Wat daaraan schort |
|---|---|---|
| Mijn taak is klaar of mislukt | pollen op `GET /api/v2/.../tasks/{id}` tot de status verandert | Alleen als hij de taak zelf startte; en na een uur is de rij weg |
| De deployment die ik zojuist bijwerkte is nu gezond | pollen op `GET /api/v2/projects/{p}/deployments/{d}`, die `errors` en `deviations` teruggeeft | Elke bevraging herberekent alles; er is geen "sinds wanneer" en geen push |
| Mijn image is uitgerold | pollen | Geen signaal; `features/upsert-deployment-api.md` beschrijft alleen de heenweg |
| Er is iets aan mijn project veranderd door iemand anders | de commits in `zad-projects` lezen | Kan alleen wie leestoegang tot de repo heeft; en de auteur is altijd dezelfde |
| Mijn project heeft een quotum of grens geraakt | nergens | Bestaat niet |

Er is dus geen enkel push-kanaal richting een agent, en het enige pull-kanaal is een taakstatus met een uur bewaartijd.

---

## De gebeurtenissen die vandaag verloren gaan en het duurst zijn om te missen

Zeven, geordend naar wat het al heeft gekost of aantoonbaar had kunnen kosten. Dit is de rangschikking die de fasering in deel 3 stuurt.

**1. Wie deed wat, bij een beveiligingsincident.** De post-mortem `docs/post-mortems/user-impersonation-oidc-email-claim.md` beschrijft een lek waarmee iedereen die via SSO-Rijk kon inloggen zich als een ander kon voordoen, in het ergste geval als beheerder. De publieke melding aan gebruikers (`docs/post-mortems/melding-zad-gebruikers.md`) zegt: "We hebben op dit moment geen aanwijzing dat het is misbruikt, en we onderzoeken nog of we dat sluitend kunnen onderbouwen." In de tijdlijn staat een regel op `[loopt]`: "Controle toegang tot Wies/ZAD/Keycloak wijzigingen". Dat onderzoek is precies wat een gebeurtenissenlog beantwoordt en wat vandaag onbeantwoordbaar is: de commits dragen een identiteit, de tabel die de actor draagt wordt na een uur geleegd, en de Keycloak-auditevents stonden op bestaande realms nog niet aan. Dit is de duurste, want de kosten zijn al gemaakt.

**2. Een projectbestand dat weken op een schemafout strandt.** Het commentaar in `opi/core/git_monitor.py:144-149` benoemt het zelf: "the old silence meant 22 production files were being skipped here with nobody able to see it". Er is nu een ERROR-logregel, en die haalt via de log watcher zelfs ntfy, maar de gebruiker van dat project ziet er niets van. Zolang de gebruiker het niet ziet is de weg terug een support-gesprek.

**3. Een bezorging die stil verdwijnt.** `TODO.md` punt 26, onderdeel 2, met de meting erbij: de relay adresseert de DSN aan `noreply-rijksapp+<project>@rijksoverheid.nl`, de upstream weigert dat adres ook, de relay noteert "discarding message after double bounce" en gooit het weg, en de relaylog bewaart drie uur. Het punt zegt letterlijk: "Een project hoort dus niets, wij ook niet". Het voorgestelde noodverband in dat punt is een alert op een mislukte bezorging, en dat is een gebeurtenis.

**4. Een commit die het cluster nooit bereikt.** `TODO.md` punt 27, bewezen op 21 augustus 2026: PR #168 zette de mailrelay uit "tot de RCA rond is", en die commit heeft het cluster nooit bereikt omdat `bootstrap/rig-system/kustomize/overlays/odcn-production` met de hand wordt toegepast. OPI wees die hele periode naar de crashende relay terwijl iedereen dacht dat de dienst uit stond. Het punt eindigt met een open beslissing: "waar de melding landt". Ook dat is een gebeurtenis.

**5. Data die 's nachts verdwijnt.** De reconciliatiescheduler purget databases, buckets en PVC's die uit een projectbestand zijn verdwenen (`opi/core/reconciliation_scheduler.py`, `opi/jobs/reconciliation.py`, zie `features/service-orphan-reconciliation.md` en `features/yaml-diff-driven-deletion.md`). De uitkomst is een logregel met drie tellingen, en de rij in `marked_for_deletion` die het voornemen droeg verdwijnt bij het opruimen. Er blijft dus niets over dat zegt dat er iets is weggegooid, laat staan wat.

**6. Een grens die vannacht is verhoogd.** De resource-tuner en de OOM-watcher wijzigen zelfstandig geheugen- en CPU-grenzen. Dit is het enige onderwerp waar wel iets bewaard blijft (`resources.history` in het projectbestand, met reden), en het laat precies zien waar het aan schort: geen actor, alleen zichtbaar voor wie het projectbestand openslaat, en niet bevraagbaar over projecten heen.

**7. Een deployment die al drie dagen rood is.** Omdat `errors` en `deviations` per paginabezoek worden herberekend, is er geen enkele manier om vast te stellen hoe lang iets al stuk is, en dus ook geen manier om te merken dat niemand ernaar heeft gekeken.

---

## De groepering naar type

Dit is de knop waar iemand straks per stuk aan draait. Het aantal is een echt ontwerpbesluit: te veel typen geeft een instellingenscherm dat niemand doorloopt, te weinig geeft een aan-uitknop die niemand gebruikt.

**Voorstel: twaalf typen.** Dat is de uitkomst van drie regels, in deze volgorde:

1. **Een type per beslissing die een redelijk mens anders zou nemen.** Als niemand denkbaar is die A wel wil en B niet, horen A en B in een type. "Backup mislukt" en "herstel mislukt" zitten daarom samen; "mijn eigen taak is klaar" en "er is iets met mijn project gebeurd" niet.
2. **Nooit meer typen dan er gebeurtenissen zijn.** Een type dat drie keer per jaar vuurt is een regel in een scherm die 362 dagen niets doet. Die gaat bij een buur in.
3. **De ernst is geen type.** Ernst is een eigenschap van de gebeurtenis, en die verschilt binnen een type (een uitrol kan slagen of falen). Wie alleen storingen wil, zet een filter op ernst en niet twaalf knoppen om.

| # | Type (VOORSTEL) | Wat erin zit | Publiek | Standaard voor projectbeheerder | Standaard voor projectlid |
|---|---|---|---|---|---|
| 1 | `uitrol` | de uitrolgroep uit paragraaf 1, plus ArgoCD-renderfouten en een afgekeurd projectbestand | A, B en C | postvak + mail bij mislukking | postvak |
| 2 | `verwijdering` | de verwijdergroep uit paragraaf 1 | A en C | postvak + mail | postvak |
| 3 | `gezondheid` | OOM, image-pull, crashlus, probe-kill, uitgeschakelde component, een mail van het project die niet is bezorgd | A en C | postvak + mail | postvak |
| 4 | `platform-ingreep` | automatische stemmer, slaapstand, weesopruiming op een resource van een project, sleutelrotatie | A, B en C | postvak + mail | geen |
| 5 | `gegevens` | backup, herstel, kloon, schemabeheer, bewaartermijn | A, B en C | postvak + mail bij mislukking | geen |
| 6 | `aanvraag-ingediend` | een goedkeuring wacht op MIJ | B | postvak + mail | n.v.t. |
| 7 | `aanvraag-besloten` | mijn aanvraag is goedgekeurd of afgewezen | A | postvak + mail | postvak |
| 8 | `leden-en-toegang` | leden, rollen, uitnodigingen ingewisseld, cross-domain-access | A en B | postvak + mail | postvak |
| 9 | `dienstwijziging` | dienst toegevoegd of verwijderd, dienstconfiguratie, bijlagen | A en C | postvak | geen |
| 10 | `werkomgeving` | databaseconsole, ad-hoc jobs | A, B en C | postvak | geen |
| 11 | `platform-mededeling` | release, onderhoud, clusterbrede berichten | A en B | postvak + mail | postvak + mail |
| 12 | `beheer-en-beveiliging` | gebruikersbeheer, de weessweep en de reconciliatie zelf, drift, scanbevindingen, geweigerde sleutels en tokens, allowlist-weigeringen, de takenmachinerie van OPI | B | n.v.t. | n.v.t. |

**Hoe de kolom `Publiek` is afgeleid.** Elke gebeurtenis uit de catalogus valt in precies een type; dat is wat regel 2 hierboven afdwingt, want een type dat te klein is gaat bij een buur in. De kolom `Publiek` is daarom geen tweede oordeel naast de tabellen maar de VERENIGING van de `publiek`-waarden van de rijen die in dat type vallen. Dat is dezelfde reden waarom het publiek in die tabellen een kolom is en geen tweede lijst: twee lijsten over dezelfde gebeurtenissen lopen gegarandeerd uit de pas, en dat geldt hier net zo goed. Wie het wil narekenen heeft aan de kolom `Wat erin zit` genoeg, op vier plekken na waar de toewijzing niet uit die kolom volgt:

- De vier algemene taakrijen uit paragraaf 1 (mislukt, hersteld na een mislukking, klaar terwijl de aanvrager weg is, afgebroken; alle vier publiek A en C) horen niet bij een eigen type maar bij de vijf taakgroepen uit diezelfde paragraaf: Uitrol bij type 1, Verwijderen bij type 2, Gegevens bij type 5, Diensten bij type 9 en Slaapstand bij type 4. Dat breekt de regel niet, want die vier zijn zelf verzamelrijen: elk staat voor 23 taaksoorten, en het is de taaksoort die het type bepaalt. Daarom draagt type 4 publiek C, en daarom draagt type 10 het niet uit die hoek maar uit zijn eigen rijen.
- De weesopruiming komt in twee omschrijvingen terug. De grens loopt bij het onderwerp: raakt de ingreep een resource van een project, dan is het type 4 (`orphan_candidate`, en de definitieve verwijdering van een gemarkeerde resource); gaat het over de sweep zelf, dan is het type 12 (`in_use_anomaly`, een teruggedraaide markering, een overgeslagen reconciliatieronde). Diezelfde grens legt de twee taakrijen die over de takenmachinerie van OPI gaan (de herstellus, de weggegooide wizardtaak) in type 12 en niet in type 1.
- De negen rijen van paragraaf 10 staan nergens als groep genoemd. Ze landen zo: het afgekeurde projectbestand in type 1, de niet bezorgde mail in type 3, de sleutelrotatie in type 4, de nieuwe clonegeneratie in type 5, `cross-domain-access` in type 8, de bijlage en de dienstwijziging in type 9, en de bootstrap-drift en het volle cluster in type 12.
- De ene rij van paragraaf 9 (OPI logt zelf een ERROR of CRITICAL) staat in geen enkele omschrijving. Hij hoort in type 12: paragraaf 9 zegt er zelf bij dat dit publiek B is en niet A, en het gaat over de machinerie van OPI en niet over een project. Daarmee telt type 12 drieentwintig catalogusrijen: vijftien uit paragraaf 7 (`:292` tot en met `:308`, op de twee mededelingsrijen `:304` en `:305` na, die in type 11 vallen), twee uit paragraaf 1, drie uit paragraaf 4, deze ene, en twee uit paragraaf 10. Dat aantal doet ertoe in deel 2: de langere bewaartermijn geldt zeven van die drieentwintig (vijf vandaag, en twee zodra ZAD ze zelf vastlegt) en niet het type als geheel, en hij geldt daarnaast rijen die helemaal niet in type 12 zitten.

**Wat een `B` op een type wel en niet betekent.** Dat de platformbeheerder rijen in dat type heeft staan, niet dat het type van hem is. Alleen de typen 6 en 12 zijn helemaal zonder publiek A. Bij de typen 1, 4, 5, 8, 10 en 11 zit de `B` op een deel van de rijen ("het geheugenplafond is bereikt" in type 4, "een geplande backup is niet eens gestart" in type 5, "een inwisseling is geweigerd" in type 8, "een databaseconsole is gestart" in type 10), en de projectbeheerder ziet dezelfde gebeurtenis met een eigen drempel. Dat is wat de leeswijzer bedoelt met "elk met een eigen drempel".

Type 12 is alleen zichtbaar voor platformbeheerders; type 6 alleen voor wie beoordeelt. Dat type draagt daarom publiek B en in dezelfde rij toch een standaard voor de projectbeheerder, en dat is geen tegenspraak: wie beoordeelt is een eigenschap van de goedkeuring en niet van de rol. `ApproverScope` kent naast `PLATFORM_ADMIN` ook `PROJECT_ADMIN` en `PROJECT_MEMBER` (`opi/services/catalog/approval.py:45-56`), en `service_use_approval` neemt de beoordelaar als parameter met `PLATFORM_ADMIN` alleen als standaardwaarde (`:170-176`). Vandaag staan alle drie de gedeclareerde goedkeuringen op `PLATFORM_ADMIN` (paragraaf 2), dus vandaag is de vereniging over de rijen `B`; zodra de eerste goedkeuring binnen een project komt te liggen komt A erbij. De standaard staat er alvast omdat het scherm over typen gaat en niet over de goedkeuringen die er toevallig zijn; deel 3 werkt dat uit bij het voorkeurenscherm. Voor de andere tien geldt de gewone regel: je ziet wat er met jouw projecten gebeurt. In type 12 zitten ook de zeven rijen uit paragraaf 7 die een recht, een account of een toegangspoging betreffen (de zevende is `:303`, en dat is naar deel 2 de zwaarste RECHTENwijziging die dit platform kent), en die krijgen in deel 2 een eigen, langere bewaartermijn. Dat is een termijn op die SOORTEN en niet op het type: de zestien andere rijen van type 12 zijn gewoon beheer (een subdomein is geclaimd, een certificaat verloopt, de reconciliatie sloeg een ronde over) en volgen de gewone klok, met de actor gepseudonimiseerd na 90 dagen. Andersom geldt die langere termijn ook buiten dit type, namelijk op alle negen rijen van paragraaf 6 (type 8) en op `cross-domain-access` uit paragraaf 10. Deel 2 punt 6 beslecht dat, geeft het criterium (een recht, een account of een toegangspoging) en de gesloten lijst met soorten.

**Waarom niet minder.** Vier of vijf typen ("uitrol, gezondheid, aanvragen, beheer") leest prettiger maar levert een scherm op waar de enige zinnige handeling is om alles aan te laten. De typen 4 en 5 zijn precies de twee waarvan de opdrachtgever zegt dat de eigenaar ze achteraf moet weten, en die moet je apart kunnen aanzetten zonder ook elke geslaagde uitrol binnen te krijgen.

**Waarom niet meer.** Per taaksoort (23), per dienst (23 diensttypen in `ServiceType`, `opi/services/services_enums.py:4`) of per `failure_type` levert honderden knoppen op. Ergens moet de grens liggen en dit is een verdedigbare plek. Wie meer verfijning wil, krijgt hem in de gebeurtenis zelf (die draagt de taaksoort en de dienst) en niet in het instellingenscherm.

**De open beslissing.** Twaalf is een voorstel, geen wet. Wat de opdrachtgever hier moet beslissen is niet het getal maar de regel eronder: **draait iemand per type aan een knop (aan/uit), of per type per kanaal (postvak / mail / Mattermost)?** Het tweede is wat GitHub doet en wat de wens beschrijft; het is ook meer scherm. De aanbeveling staat in deel 3: per type per kanaal, maar met werkbare standaarden per rol zodat niemand het scherm hoeft te openen om iets zinnigs te krijgen.

## Wat hier bewust niet in staat

- **Metrieken en drempelwaarden.** "CPU boven 80 procent", "de wachtrij loopt op", "drie backups op rij gemist" is bewaking en geen gebeurtenis. Dat hoort in een metriek met een regel eroverheen; Prometheus draait al en Alertmanager is de ontbrekende helft. Zie deel 3, Kanaal 6.
- **Applicatielogs van de klant.** Wat er in de container van een project gebeurt is van dat project. ZAD meldt over het platform en over de deployment, niet over de applicatie.
- **De inhoud van de meldingsteksten.** Wat er precies staat is werk voor de bouwfase, met een regel die nu al vastligt: de gebruiker wordt aangeschreven met "je".

## Wat niet is geverifieerd

- Of de Keycloak-auditevents op de draaiende productierealms aan staan. Dat vraagt een blik op het cluster.
- Of de standaardretentie van een uur op `async_tasks` op productie inderdaad geldt. Er staat geen `TASK_WORKER_CLEANUP_RETENTION_HOURS` in `bootstrap/rig-system/kustomize/`, dus de standaardwaarde uit `opi/core/config.py:369` is de waarschijnlijke waarde, maar een omgevingsvariabele elders kan hem alsnog zetten.
- De retentie van de externe Loki-installatie en de node-log-rotatie. `plans/technische-review-bio-en-nora-bevindingen.md` noemt ongeveer drie uur voor de node-log-rotatie en stelt vast dat beide buiten deze repo staan en niet te verifieren zijn. De drie uur uit `TODO.md` punt 26 gaat over de relaylog, niet over de OPI-log.
- Of er buiten dit versiebeheer nog een meldkanaal bestaat (een dashboard, een handmatige Grafana-alert). Binnen de repo is ntfy het enige.
- De ankers in dit document zijn gemeten op `83ac4b9b` door de twee bronstukken en zijn bij de samenvoeging in RC-163 niet opnieuw gemeten. Wie een regel gebruikt als grondslag voor bouwwerk, meet hem tegen de tak waar hij op bouwt. Datzelfde geldt sterker voor de kolom `publiek` en voor de vijftien rijen uit de Herkomst-alinea (`:13`). De kolom `publiek` staat op alle negenenzeventig rijen terwijl de bronstukken samen hoogstens vijfendertig van die waarden konden leveren, dus voor minstens vierenveertig rijen is hij in RC-163 toegekend, en de achtentwintig gecombineerde waarden (`A en B`, `A en C`) staan in geen enkel bronstuk. Bij die vijftien rijen zijn daarnaast de kolommen belanghebbenden, ernst en standaardkanaal toegekend en niet gemeten, omdat het bronstuk die kolommen niet had.
