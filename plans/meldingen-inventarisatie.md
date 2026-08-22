# Meldingen in ZAD: de inventarisatie

**Geschreven op**: 22 augustus 2026, tegen commit `83ac4b9b` op de tak
`meldingen-in-zad-inventarisatie-en-plan-van-aanpak`. Elk anker in de tabellen hieronder is
nagelopen tegen de code. Wat er niet is staat als **bestaat nog niet**, en niet als aanname.

Dit is deel 1 van drie:

| Deel | Document |
|---|---|
| 1. Welke events hebben we | dit document |
| 2. Hoe leggen we ze slim vast | `plans/meldingen-oplossingsrichtingen.md` |
| 3. Hoe melden we, en het plan | `plans/meldingen-plan-van-aanpak.md` |

## De stand van zaken in een alinea

ZAD heeft geen meldingssysteem. Gemeten: er is geen tabel, geen model, geen route en geen
sjabloon dat er over gaat. `grep -rn notification --include="*.py" opi/` levert **elf treffers
in zes bestanden** op, en die gaan alle over de logbewaker en ntfy
(`opi/services/log_watcher.py`, `opi/core/logwatcher_scheduler.py`, `opi/core/config.py:440`,
`opi/core/simple_background.py:117`) of over het contactadres voor de certificaten van Let's
Encrypt (`opi/api/router.py:1012`, `opi/core/cluster_config.py:1009`).

Laat je de beperking tot `.py` weg (`grep -rn notification opi/`), dan zijn het **achttien
treffers in acht bestanden**: er komen zes regels bij in
`opi/templates_lotc/bg/feedback.html.j2` en één in
`opi/services/log_watch_ignore_patterns.txt`. Die zes zijn de LOTC-componenten
`c-notification` en `c-notification-item` op de proefopstelling, en dat is een *vluchtige
bevestiging na een actie*, geen postvak; de naamsbotsing staat in
`plans/meldingen-plan-van-aanpak.md` onder Kanaal 1. De conclusie verandert er niet door: er
is niets om op voort te bouwen en ook niets om te slopen.

Er zijn vandaag precies **drie manieren** waarop iemand te weten komt dat er iets gebeurd is:

1. **Hij kijkt op het juiste scherm op het juiste moment.** De projectdetailpagina, de
   deploymentkaart, de takentabel, `/admin/approvals`. Alles is trekwerk: de pagina vraagt,
   het scherm antwoordt, en wie niet kijkt weet niets.
2. **Hij volgt een taak die hij zelf startte.** Het voortgangsvenster ververst zichzelf met
   `hx-trigger="every 2s"` (`opi/templates_lotc/partials/task_progress_fragment.html.j2:34`)
   tot de taak klaar of mislukt is. Dat werkt goed, maar het is gebonden aan het tabblad dat
   op dat moment openstaat.
3. **Hij is de platformbeheerder die ntfy leest.** De logbewaker
   (`opi/services/log_watcher.py`) stuurt ERROR- en CRITICAL-regels uit het OPI-log naar een
   ntfy-topic. Dat is de enige weg waarlangs ZAD vandaag uit zichzelf iets naar buiten duwt.

### Waarom "op het scherm kijken" hier niet genoeg is, in één getal

`TASK_WORKER_CLEANUP_RETENTION_HOURS` staat op **1** (`opi/core/config.py:369`). De
opruimlus van de takenwerker (`opi/core/task_worker.py:420`) verwijdert elke taak in een
eindtoestand waarvan `completed_at` ouder is dan dat venster
(`opi/core/async_task_service.py:670`). Een deployment die vannacht om drie uur mislukte,
bestaat om vier uur nergens meer: niet in de takentabel, niet in de API, nergens. De
uitzondering die er is bewijst dat het probleem bekend is: een uitgestelde uitrol wordt
expliciet gespaard, "want drift die na een week verdwijnt is precies de stille drift die dit
moet laten zien". Dezelfde redenering geldt voor een mislukte deploy, en daar geldt hij niet.

**Dat is de kern van de opdracht.** Het gaat niet alleen om aflevering per e-mail of
Mattermost. Het gaat er eerst om dat een gebeurtenis ergens BLIJFT staan, lang genoeg dat
iemand hem de volgende ochtend nog kan zien.

## Hoe je de tabellen leest

| Kolom | Betekenis |
|---|---|
| event | de gebeurtenis, in gewone taal |
| bron | het codeanker waar hij vandaag ontstaat, of **bestaat nog niet** |
| onderwerp | waar hij over gaat: project / deployment / component / dienst / gebruiker / platform |
| belanghebbenden | wie er iets aan heeft, in rollen |
| ernst | ter informatie / actie nodig / storing |
| standaardkanaal | wat een redelijke standaard is, en waarom |

**"Bestaat nog niet" heeft twee smaken** en het verschil is belangrijk voor het bouwwerk:

- *de toestand bestaat, de gebeurtenis niet*: de code weet het moment wel, maar er is geen
  plek waar hij het meldt. Bijvoorbeeld een mislukte taak: de status gaat naar `failed`, en
  daar houdt het op. Dit is goedkoop: er hoeft alleen een aanroep bij.
- *de toestand bestaat ook niet*: er is niets dat het waarneemt. Bijvoorbeeld een
  certificaat dat bijna verloopt. Dit is duur: er moet eerst iets gebouwd worden dat kijkt.

Waar het uitmaakt staat het erbij.

### De rollen

Uit de code, niet verzonnen:

| Rol | Waar hij vandaan komt |
|---|---|
| **platformbeheerder** | `UserService.is_platform_admin()`, een allowlist op e-mailadres |
| **projectbeheerder** | rol `admin` of `owner` in het projectbestand; `PROJECT_EDIT_ROLES` in `opi/services/project_authorization.py:27` |
| **projectlid** | rol `member` of `developer`; het schema kent vier rollen (`admin`, `owner`, `member`, `developer`, `opi/schemas/project_v2.json`) |
| **actor** | degene die de handeling zelf uitvoerde. Vaak identiek aan een van de andere rollen, maar apart genoemd omdat "jouw eigen actie is klaar" een andere melding is dan "er is iets met jouw project gebeurd" |

Let op: de rol **actor** is vandaag maar half vastgelegd. Taken dragen `created_by`
(`opi/core/async_task_service.py:113`), runs dragen `started_by` en `ended_by`
(`opi/services/persistence/runs.py`), maar een goedkeuringsoordeel legt alleen `by` in de
history vast en een wijziging aan het projectbestand landt in de git-commit. Wie iets deed is
dus per bron ergens anders opgeslagen, en dat is werk voor de bouwfase.

### De standaardkanalen die in de tabellen voorkomen

`postvak` is het meldingenoverzicht in ZAD zelf (bestaat nog niet, zie deel 3). `mail` is
e-mail naar de persoon. `ntfy` is het bestaande beheerderskanaal. `geen` betekent: wel
vastleggen, niet actief melden; je ziet het als je gaat kijken. Waar "postvak + mail" staat
is de mail bedoeld als standaard AAN voor die rol, met de mogelijkheid hem uit te zetten.

---

## 1. Asynchrone taken

**Waar**: `opi/core/async_task_service.py` (`TaskType` op regel 54, `AsyncTaskStatus` op
regel 80), uitgevoerd door `opi/core/task_worker.py`. De resultaatmodellen per soort staan in
`opi/api/task_models.py`. Documentatie: `features/async-task-system.md`,
`features/task-progress-view.md`, `features/task-steps.md`.

Er zijn **23 taaksoorten** en **zes toestanden** (`pending`, `claimed`, `running`,
`completed`, `failed`, `cancelled`). Dat is niet 138 meldingen: de meeste overgangen zijn
mechaniek en geen nieuws. Wat wel nieuws is:

| event | bron | onderwerp | belanghebbenden | ernst | standaardkanaal |
|---|---|---|---|---|---|
| Een taak is mislukt | `async_task_service.py:329` (`fail_task`); aangeroepen vanuit `task_worker.py:208` | deployment of project, afhankelijk van de soort | actor, projectbeheerder | storing | postvak + mail |
| Een taak is klaar na eerder te zijn mislukt (herstel) | `async_task_service.py:313` (`complete_task`), in combinatie met `attempt` uit dezelfde tabel | idem | actor, projectbeheerder | ter informatie | postvak |
| Een langlopende taak is klaar terwijl de aanvrager weg is | `async_task_service.py:313` | idem | actor | ter informatie | postvak |
| Een taak is afgebroken | `async_task_service.py:399` (`update_task_status`) | idem | actor | ter informatie | postvak |
| Een taak is vastgelopen en teruggezet door de herstellus | `async_task_service.py:449` (`recover_stale_tasks`), lus in `task_worker.py:405` | platform | platformbeheerder | actie nodig | ntfy |
| Een wijziging staat al langer klaar zonder uitrol | `async_task_service.py:592` (`get_deferred_rollouts`) | project | projectbeheerder | actie nodig | postvak |

**De belangrijkste bevinding hier is niet een gemis maar een houdbaarheidsdatum.** De
melding "je deploy is mislukt" is in de code wel te maken (er staat een `failed` met een
foutmelding), maar de drager ervan verdwijnt na een uur. Elke oplossing die de taakrij als
opslag gebruikt, erft dat uur.

**Welke van de 23 soorten een eigen meldingstype verdienen.** Niet alle 23: `refresh_project`
en `refresh_deployment` zijn onderhoud, `configure_service_values` is een instelling opslaan.
De soorten waar de uitkomst voor iemand anders dan de indiener uitmaakt:

| Groep | Taaksoorten |
|---|---|
| Uitrol | `create_project`, `upsert_deployment`, `update_image`, `add_component`, `add_component_to_deployment`, `update_component`, `refresh_deployment`, `refresh_project` |
| Verwijderen | `delete_project`, `delete_deployment`, `delete_component`, `delete_attachment` |
| Gegevens | `backup`, `restore`, `clone_database`, `clone_bucket`, `manage_database_schemas` |
| Diensten | `add_service`, `configure_service`, `configure_service_values`, `configure_attachment` |
| Slaapstand | `sleep_deployment`, `wake_deployment` |

Voorstel: **niet per taaksoort een meldingstype**, maar per groep. Vijf typen in plaats van
23, en de taaksoort staat in het meldingsrecord zodat de tekst wel precies kan zijn. Zie de
groepering onderaan dit document.

---

## 2. Aanvragen en goedkeuringen

**Waar**: `opi/services/catalog/approval.py` (`ApprovalSpec`, `ApprovalStatus`,
`ApproverScope`, `service_use_approval()` op regel 170), `opi/services/approvals.py` (de
catalogusloop), `opi/web/router_approvals.py` (de beheerpagina op `/admin/approvals`).
Documentatie: `features/aanvragen-beheerpagina.md`.

Vandaag declareren **twee diensten** samen **drie goedkeuringen**:

| Dienst | Spec | Wat er goedgekeurd wordt |
|---|---|---|
| `publish-on-web` | `domain` | een eigen domeinnaam (`opi/services/catalog/publish_on_web/__init__.py:411`) |
| `publish-on-web` | `subdomain` | een subdomein onder een cluster-domein (`:420`) |
| `send-email` | `send-email` | mag dit project de dienst gebruiken (`opi/services/catalog/send_email/__init__.py:88`) |

De statussen zijn `none`, `requested`, `approved`, `denied` (`opi/services/catalog/approval.py:31`).

| event | bron | onderwerp | belanghebbenden | ernst | standaardkanaal |
|---|---|---|---|---|---|
| Een aanvraag is ingediend | `approvals.py:81` (`ensure_approval_requests`), en de dienst-eigen `_ensure_requested` in `approval.py` | dienst binnen een project | platformbeheerder (de `ApproverScope` bepaalt wie) | actie nodig | postvak + mail |
| Een aanvraag is goedgekeurd | `approvals.py:123` (`apply_approval_verdicts`) | idem | aanvrager, projectbeheerder | ter informatie | postvak + mail |
| Een aanvraag is afgewezen | `approvals.py:123` | idem | aanvrager, projectbeheerder | actie nodig | postvak + mail |
| Een aanvraag staat al lang open | **bestaat nog niet** (toestand ook niet: er is geen tijdstip van indienen, alleen de history-regels van oordelen) | idem | platformbeheerder | actie nodig | postvak |
| Een aanvraag is ingetrokken | **bestaat nog niet** (de toestand bestaat ook niet: `ApprovalStatus` kent geen `withdrawn`) | idem | platformbeheerder | ter informatie | postvak |

**Twee bevindingen.**

De opdracht noemt vier momenten die elk een melding verdienen: ingediend, goedgekeurd,
afgewezen, ingetrokken. Er zijn er **drie**. Intrekken bestaat niet als toestand: de enum in
`opi/services/catalog/approval.py:31` heeft vier waarden en `withdrawn` is er geen van. Een aanvraag intrekken zou
vandaag betekenen dat je de dienst uit het projectbestand haalt, en dan is er geen
goedkeuringsblok meer om iets over te melden. Als intrekken een melding moet worden, is dat
eerst een uitbreiding van de goedkeuringsmachine, en dat valt buiten meldingen.

En: **indienen is toestandsvormig, niet gebeurtenisvormig.** `_ensure_requested` is
uitdrukkelijk idempotent geschreven ("lees het project zoals het staat en vul aan wat
ontbreekt"), zodat een aanvraag via de API op dezelfde plek landt als een vinkje in de wizard.
Dat is goed voor de goedkeuringsmachine en lastig voor meldingen: er is geen moment "hier
werd de aanvraag ingediend", er is alleen "er staat nu een aanvraag". Wie er een melding aan
hangt moet de overgang zelf waarnemen (was er geen blok, is er nu wel) in plaats van te
kunnen aanhaken bij een emit. Dit is het scherpste voorbeeld in dit hele document van waarom
de plek waar je events laat ontstaan een echte keuze is; zie deel 2, punt 1.

**Dit is verder wel het beste startpunt voor fase 1.** Er zijn maar drie goedkeuringen, ze
zijn zeldzaam (dus geen volumeprobleem), de belanghebbende is vrijwel altijd een
platformbeheerder (dus weinig autorisatiewerk), en de pijn is echt: vandaag ziet niemand een
aanvraag tot iemand `/admin/approvals` opent.

---

## 3. Gezondheid van deployments

**Waar**: `opi/services/oom_watcher.py` (de waarneming),
`opi/services/event_interpreter.py` (de duiding), `opi/services/deployment_state.py` (wat de
diensten over een deployment weten), `opi/services/deployment_diagnostics.py` (de
afwijkingen). Documentatie: `features/image-pull-backoff-detection.md`,
`features/probe-kill-is-geen-crash.md`, `features/uitgeschakeld-is-niet-gezond.md`,
`features/status-afwijkingen.md`, `features/argocd-render-error-surfacing.md`.

`ComponentFailure.failure_type` kent drie waarden: `oom`, `image_pull`, `crash_loop`
(`oom_watcher.py:113`).

| event | bron | onderwerp | belanghebbenden | ernst | standaardkanaal |
|---|---|---|---|---|---|
| Een container is door de OOM-killer geraakt | `oom_watcher.py:199` (`check_pod_health`), detectie op `lastState.terminated.reason == "OOMKilled"` (`:293`) | component | projectbeheerder, projectlid | actie nodig | postvak + mail |
| Een image kan niet worden opgehaald | `oom_watcher.py:199`, reden uit `IMAGE_PULL_REASONS` | component | projectbeheerder, actor van de laatste image-wijziging | storing | postvak + mail |
| Een component is daarop op `replicas: 0` gezet | `oom_watcher.py:478` (`disable_components_for_image_pull`) | component | projectbeheerder | storing | postvak + mail |
| Een component crasht herhaaldelijk | `oom_watcher.py:199`, `CrashLoopBackOff`; duiding in `event_interpreter.py` (`_CRASH_TITLE`) | component | projectbeheerder, projectlid | actie nodig | postvak |
| Een container is gedood door een falende probe | `event_interpreter.py:311` (`_probe_kill_translation`) | component | projectbeheerder | actie nodig | postvak |
| ArgoCD kan de manifesten niet renderen | `event_interpreter.py:562` (`interpret_argocd_errors`), `:324` (`condense_render_error`) | deployment | projectbeheerder | storing | postvak + mail |
| Een deployment wijkt af zonder dat er iets stuk is | `deployment_diagnostics.py:262` (`gather_sync_deviations`) | deployment | projectbeheerder | ter informatie | geen |
| Een deployment gaat van gezond naar ongezond | **bestaat nog niet** (de toestand wordt per bevraging opnieuw opgehaald; er is nergens een vorige toestand om mee te vergelijken) | deployment | projectbeheerder, projectlid | storing | postvak + mail |
| Een deployment is weer gezond | **bestaat nog niet**, om dezelfde reden | deployment | projectbeheerder | ter informatie | postvak |

**De bruikbaarste vondst van deze hele inventarisatie zit hier.**
`event_interpreter.EventSeverity` (`:18`) classificeert al op `actionable` /
`informational` / `noise`. Dat is precies de as die een meldingssysteem nodig heeft, hij is
al gevuld voor de hele vertaaltabel van Kubernetes-redenen, en hij is al getoetst. Een
meldingssysteem hoeft die classificatie niet opnieuw te verzinnen: het kan hem overnemen. De
drie waarden vertalen recht toe naar de ernst-kolom hierboven (`actionable` -> actie nodig,
`informational` -> ter informatie, `noise` -> helemaal geen melding).

**En de belangrijkste beperking.** Er is geen toestandsgeheugen. `check_pod_health` kijkt
naar de pods zoals ze nu zijn; `interpret_events` leest de Kubernetes-events zoals ze nu
zijn. Nergens staat wat de vorige uitslag was. "Ging over van groen naar rood" is dus geen
gebeurtenis die je kunt afvangen, want niemand houdt bij wat groen was. Dat maakt de
gezondheidsmeldingen duurder dan ze lijken: er hoort een toestandsveld bij dat de vorige
uitslag onthoudt, anders krijg je bij elke ronde dezelfde melding opnieuw (en dat is precies
wat de dedup uit deel 2 moet opvangen).

---

## 4. Automatisch ingrijpen door het platform

Dit is de categorie waar de wens het scherpst is: het platform verandert iets aan een
deployment zonder dat de eigenaar erom vroeg, en de eigenaar hoort er niets over.

**Waar**: `opi/services/resource_tuning_service.py` (`features/auto-resource-tuning.md`),
`opi/services/catalog/sleep_mode/` (`features/sleep-mode.md`),
`opi/jobs/service_orphan_sweep.py` + `opi/jobs/reconciliation.py` +
`opi/services/marked_for_deletion_service.py` (`features/service-orphan-reconciliation.md`).

| event | bron | onderwerp | belanghebbenden | ernst | standaardkanaal |
|---|---|---|---|---|---|
| Het platform heeft het geheugen van een component bijgesteld | `resource_tuning_service.py:741` (`apply_resource_tuning`) | component | projectbeheerder | ter informatie | postvak + mail |
| Het platform kon het geheugen niet verder verhogen (plafond bereikt) | `resource_tuning_service.py` via `get_max_memory_limit_mi` uit `opi/core/cluster_config.py` | component | projectbeheerder, platformbeheerder | actie nodig | postvak + mail |
| Een deployment is in slaapstand gezet | `opi/services/catalog/sleep_mode/scheduler.py` (`plan_sweep` + de lus die hem toepast) | deployment | projectbeheerder, projectlid | ter informatie | postvak |
| Een deployment is gewekt | idem | deployment | projectlid | ter informatie | geen |
| Een deployment bleef hangen in "wakker worden" en is teruggezet | idem (de `waking`-tak van de sweeper) | deployment | projectbeheerder | actie nodig | postvak |
| Een resource is als wees aangemerkt | `opi/jobs/service_orphan_sweep.py` (classificatie `orphan_candidate`) | dienst binnen een project | platformbeheerder, projectbeheerder | actie nodig | postvak + mail |
| Een resource lijkt wees maar is in gebruik (`in_use_anomaly`) | `opi/jobs/service_orphan_sweep.py` | dienst | platformbeheerder | actie nodig | ntfy |
| Een gemarkeerde resource is definitief verwijderd | `opi/jobs/reconciliation.py:219` (`_purge_marks`), via `opi/services/marked_for_deletion_service.py` | dienst | projectbeheerder | storing (onomkeerbaar) | postvak + mail |
| Een markering is teruggedraaid omdat de resource terugkwam | `opi/jobs/reconciliation.py:231` (de `unmarked`-tak in `_purge_marks`) | dienst | platformbeheerder | ter informatie | geen |

**De automatische stemmer verdient een aparte opmerking.** Hij schrijft in het
projectbestand, inclusief een `history`-blok met tijdstip, bron (`auto-tune`) en reden. Dat
is het enige plekje in de hele inventarisatie waar een automatische ingreep vandaag al een
duurzaam spoor achterlaat dat de gebruiker kan lezen. Het staat alleen in de YAML, en niemand
krijgt te horen dat er een regel bij kwam. Voor meldingen is dat goed nieuws: de gegevens die
in de melding moeten staan (wat, hoeveel, waarom) worden al vastgelegd.

**En het onomkeerbare geval.** "Een gemarkeerde resource is definitief verwijderd" is de
enige regel in dit hele document waar de melding ACHTER de daad aankomt en er niets meer aan
te doen is. Dat pleit ervoor dat dit type niet uitzetbaar is; zie deel 3, "Voorkeuren".

---

## 5. Backups en herstel

**Waar**: `opi/core/backup_scheduler.py` (de planner),
`opi/core/backup_retention_sweep.py` (de opruiming), `opi/core/task_handlers_backup.py` (de
uitvoering), `opi/manager/backup/`. Documentatie: `features/backup-system.md`,
`features/scheduled-backups.md`, `features/backup-retention-sweep.md`.

De planner maakt taken van het type `backup` aan; de uitvoering loopt dus door de takenrij en
erft alles wat daar in paragraaf 1 over staat, inclusief het uur bewaartijd.

| event | bron | onderwerp | belanghebbenden | ernst | standaardkanaal |
|---|---|---|---|---|---|
| Een geplande backup is mislukt | `async_task_service.py:329` via de taak die `backup_scheduler.py` aanmaakte | deployment | projectbeheerder | storing | postvak + mail |
| Een geplande backup is niet eens gestart | `backup_scheduler.py:404` (de tak `status == "error"`: Kopia niet bevraagbaar, dus de tick wordt overgeslagen); wordt alleen `logger.warning` | deployment | projectbeheerder, platformbeheerder | storing | postvak + mail |
| Een handmatige backup is klaar | de `backup`-taak | deployment | actor | ter informatie | postvak |
| Een herstel is klaar of mislukt | de `restore`-taak | deployment | actor, projectbeheerder | storing bij mislukking | postvak + mail |
| De opruimronde heeft snapshots verwijderd | `backup_retention_sweep.py` | project | projectbeheerder | ter informatie | geen |
| Er is al N dagen geen geslaagde backup | **bestaat nog niet** (de toestand is wel te bevragen: de planner vraagt Kopia al naar de laatste geslaagde snapshot, `backup_scheduler.py:286`) | deployment | projectbeheerder | actie nodig | postvak + mail |

**"Een geplande backup die faalt is vandaag stil" klopt, en de tweede regel is erger dan de
eerste.** Als de taak faalt, staat er tenminste nog een uur een `failed`-rij. Maar als de
planner Kopia niet kan bevragen, slaat hij de tick over met een `logger.warning` en maakt hij
helemaal geen taak aan. Er is dan geen rij, geen mislukking en geen backup. Alleen een
logregel, en die haalt de ntfy-drempel niet want het is een warning en de logbewaker leest
alleen ERROR en CRITICAL (`log_watcher.py:16`).

De laatste regel ("al N dagen geen geslaagde backup") is de melding die dit gat echt dicht,
want hij hangt niet aan een gebeurtenis maar aan het UITBLIJVEN ervan. Dat is een ander soort
melding en het is de moeite waard om hem apart te noemen: hij vraagt een periodieke controle,
geen haak in een codepad.

---

## 6. Leden, uitnodigingen en toegang

**Waar**: `opi/manager/invite_manager.py`, `opi/api/invite_routes.py`,
`opi/services/catalog/invite/`, `opi/services/project_authorization.py`. Documentatie:
`features/invite-system.md`, `features/invites.md`, `features/zad-external-user-support.md`.

Belangrijk om te weten hoe dit werkt: een uitnodiging is een **gedeelde link met een code**,
geen persoonlijke uitnodiging. ZAD kent de persoon niet voor hij hem inwisselt
(`opi/services/catalog/invite/__init__.py`: "De code IS de uitnodiging"). Er is dus geen
"jij bent uitgenodigd"-melding mogelijk, want er is geen adres om hem heen te sturen.

| event | bron | onderwerp | belanghebbenden | ernst | standaardkanaal |
|---|---|---|---|---|---|
| Iemand heeft een uitnodiging ingewisseld via SSO | `invite_manager.py:262` (`complete_sso_invite`) | project | projectbeheerder | ter informatie | postvak |
| Iemand heeft een uitnodiging ingewisseld met een lokaal account | `invite_manager.py:355` (`complete_local_invite`) | project | projectbeheerder | ter informatie | postvak |
| Een inwisseling is geweigerd (verkeerd domein, verkeerde methode) | `invite_manager.py:73` (`validate_email_domain`), `:101` (`validate_auth_method`) | project | projectbeheerder | ter informatie | geen |
| Een uitnodigingscode is ongeldig of verlopen | `invite_manager.py:128` (`get_valid_invite`) | project | projectbeheerder | ter informatie | geen |
| Iemand is als lid aan een project toegevoegd | **bestaat nog niet als gebeurtenis**: dit is een wijziging aan de `users:`-lijst in het projectbestand via `opi/forms/editables/fields/team.py:22` (`USERS_SEQUENCE_EDITABLE`) en landt als git-commit | project + gebruiker | de toegevoegde persoon, projectbeheerder | ter informatie | postvak + mail |
| Iemands rol in een project is gewijzigd | **bestaat nog niet als gebeurtenis**, zelfde weg | project + gebruiker | de betrokkene, projectbeheerder | ter informatie | postvak |
| Iemand is uit een project verwijderd | **bestaat nog niet als gebeurtenis**, zelfde weg | project + gebruiker | de betrokkene, projectbeheerder | actie nodig | postvak + mail |
| Een projectrol is aan een realm-gebruiker toegekend | `invite_manager.py:187` (`assign_invite_permissions`), `:153` (`_assign_client_roles`) | gebruiker | projectbeheerder | ter informatie | geen |

**De drie ledenwijzigingen zijn het lastigste geval in deze hele inventarisatie**, en het is
de moeite waard te zien waarom. Ze zijn geen aanroep in een codepad maar een verschil tussen
twee versies van een YAML-lijst. Wie er een melding aan wil hangen moet de oude en de nieuwe
`users:` vergelijken op het moment van opslaan. Dat kan (de opslagroute heeft beide in
handen), maar het is een ander soort werk dan een `emit()` neerzetten, en het geldt voor elk
veld in het projectbestand dat ooit een melding moet worden. Zie deel 2, punt 1, de variant
"de opslagweg vergelijkt".

En let op de asymmetrie: "je bent lid geworden" gaat naar iemand die op dat moment nog geen
lid was, en "je bent verwijderd" naar iemand die het niet meer is. Beide vallen buiten de
gewone autorisatieregel "je ziet meldingen van je eigen projecten". Dat is precies de vraag
die in deel 2 bij richting C ("wat te doen met iemand die na het feit lid wordt of geen lid
meer is") beantwoord moet worden, en de reden dat het antwoord niet "we bevragen op het
moment van kijken" kan zijn.

---

## 7. Beheerdersgebeurtenissen

**Waar**: `opi/services/user_admin_service.py` (`features/user-admin-crud.md`),
`opi/services/user_service.py` (de platform-adminlijst), en voor de clusterbrede zaken
grotendeels: buiten OPI.

| event | bron | onderwerp | belanghebbenden | ernst | standaardkanaal |
|---|---|---|---|---|---|
| Een gebruiker is aangemaakt in het platformregister | `user_admin_service.py:41` (`create_user`) | gebruiker | platformbeheerder | ter informatie | postvak |
| Een gebruiker is gewijzigd | `user_admin_service.py:51` (`update_user`) | gebruiker | platformbeheerder | ter informatie | geen |
| Een gebruiker is verwijderd | `user_admin_service.py:65` (`delete_user`) | gebruiker | platformbeheerder | actie nodig | postvak |
| Iemand is platformbeheerder geworden of afgevoerd | **bestaat nog niet**: de allowlist komt uit de configuratie (`UserService.is_platform_admin`), niet uit een handeling in de applicatie | platform | platformbeheerders | actie nodig | postvak + mail |
| Een subdomein is geclaimd of vrijgegeven | `opi/services/persistence/subdomain_registry.py:211`, `:301`, `:320`, `:464` (schrijft naar de logger `opi.audit.subdomain`) | platform | platformbeheerder | ter informatie | geen |
| Er is een nieuwe release van het platform | **bestaat nog niet**: er is een `/version`-endpoint met de bouwgegevens, maar niets dat een wijziging daarvan waarneemt | platform | iedereen | ter informatie | postvak |
| Onderhoud is gepland | **bestaat nog niet**, in geen enkele vorm | platform | iedereen | actie nodig | postvak + mail |
| De beveiligingsscan heeft bevindingen | **bestaat nog niet in OPI**: de scan draait in GitHub Actions (`.github/workflows/security.yml`, zie `features/security-scanning-pipeline.md`) en meldt in GitHub, niet in ZAD | platform | platformbeheerder | actie nodig | (zie hieronder) |
| Een gebruikte image is verouderd | **bestaat nog niet als lopend proces**: `features/image-version-audit.md` is een handmatig onderzoek van februari 2026, geen controle die draait | platform | platformbeheerder | actie nodig | (zie hieronder) |
| Een certificaat verloopt binnenkort | **bestaat nog niet**: het contactadres in de clusterconfiguratie is dat van de ACME-account, dus Let's Encrypt mailt rechtstreeks en ZAD weet er niets van | platform | platformbeheerder | actie nodig | (zie hieronder) |

**Over de laatste drie.** Dit zijn geen meldingen die ZAD kan afvangen, want het zijn geen
gebeurtenissen in ZAD. Ze horen in de inventarisatie omdat ze in de opdracht staan en omdat
het antwoord "buiten de deur" een echt antwoord is. Wie ze binnen ZAD wil hebben, bouwt
eerst iets dat kijkt (een GitHub-webhook die de scanuitslag binnenhaalt, een periodieke
vergelijking van draaiende images tegen upstream, een controle op de vervaldatum van de
certificaten die het cluster serveert). Dat is per stuk een eigen opdracht, en het is
verstandig dat ze niet in fase 1 zitten.

Wat wel in het plan hoort: **de meldingsmachine moet events van buiten kunnen aannemen.** Als
een GitHub Action een bevinding kan POSTen naar een intern endpoint, is de scanmelding een
kwestie van dat endpoint en niet van een nieuw meldingssysteem. Dat is een goedkoop
ontwerpbesluit dat nu genomen moet worden en later duur is om alsnog in te bouwen.

---

## 8. Kortlopende workloads

**Waar**: `opi/services/runs_service.py`, `opi/services/persistence/runs.py`,
`opi/core/db_console_reaper.py`, `opi/manager/db_console_manager.py`.

`RunKind` kent `db-console` en `job` (gepland, `runs_service.py:25`). `RunStatus` kent
`starting`, `running`, `succeeded`, `failed`, `stopped`, `expired` (`:32`).

| event | bron | onderwerp | belanghebbenden | ernst | standaardkanaal |
|---|---|---|---|---|---|
| Een databaseconsole is gestart | `runs_service.py:46` (`create_run`) | deployment | projectbeheerder | ter informatie | postvak |
| Een console is beëindigd door de gebruiker | `runs_service.py:124` (`mark_ended`, status `stopped`) | deployment | actor | ter informatie | geen |
| Een console is verlopen en opgeruimd | `db_console_reaper.py`, via `mark_ended` met status `expired` | deployment | actor | ter informatie | postvak |
| Een run is mislukt | `runs_service.py:124` met status `failed`, veld `error_message` | deployment | actor, projectbeheerder | actie nodig | postvak |
| Een ad-hoc job is klaar | **bestaat nog niet**: `RunKind.JOB` staat in de enum met het commentaar "planned: ad-hoc pod running an image + command" | deployment | actor | ter informatie | postvak |

**Een console starten is een beheerdersgebeurtenis vermomd als gebruikersgebeurtenis.**
Iemand opent een directe verbinding met de productiedatabase van een project. De `runs`-tabel
legt `started_by`, `started_at` en `ended_by` vast, en de tabel wordt niet opgeruimd (anders
dan taken), dus het spoor blijft. Dat is een goede reden om het als melding aan de andere
projectbeheerders te tonen: niet omdat er iets mis is, maar omdat het het soort handeling is
waar collega's van horen te weten. Dit is meteen het duidelijkste voorbeeld van een event dat
tegelijk in het meldingssysteem EN in een audittrail thuishoort; zie deel 2, punt 4.

---

## 9. Wat er al één kanaal heeft: de logbewaker en ntfy

**Waar**: `opi/services/log_watcher.py` (de pijplijn),
`opi/core/logwatcher_scheduler.py` (de planner in de applicatie),
`scripts/log_watch/watch.py` (de losse CLI met Claude-triage).

Wat het doet, in vier stappen (`log_watcher.py:16`): het bevraagt Loki via de
Grafana-datasource-API op ERROR- en CRITICAL-regels uit de OPI-container over een venster van
35 minuten, gooit alles weg dat op de negeerlijst staat, ontdubbelt de rest tegen een
toestand (standaard 6 uur, `LOGWATCHER_DEDUP_HOURS`), en POST wat overblijft naar ntfy.

| Eigenschap | Waarde | Waar |
|---|---|---|
| Standaard aan | nee, `LOGWATCHER_ENABLED = False` | `opi/core/config.py:441` |
| Interval | 1800 seconden | `opi/core/config.py:442` |
| Server | `https://ntfy.sh` | `opi/core/config.py:444` |
| Ontdubbelvenster | 6 uur | `opi/core/config.py:448` |
| Onderwerp | een geheim, onraadbaar topic ("treat like a password") | `opi/core/config.py:443` |
| Toestand | een dict in het geheugen van de planner, verdwijnt bij een herstart | `logwatcher_scheduler.py:32` |

**Hoe dit zich tot het nieuwe verhoudt: naast elkaar, niet erin op.** Drie redenen, en ze
zijn alle drie hard.

1. **Het publiek is anders.** De logbewaker meldt fouten uit het OPI-logboek: stacktraces,
   uitzonderingen, dingen die van ons zijn en niet van de klant. Een projectbeheerder heeft er
   niets aan en zou er niets van moeten zien. Dit is ops, niet klant.
2. **De bron is anders.** Alle events in dit document ontstaan in de code van OPI zelf, op
   een moment dat OPI kent. De logbewaker ontstaat in Loki, achteraf, uit tekst. Dat is een
   fundamenteel andere pijplijn (bevragen, ontdubbelen op een genormaliseerde tekstsleutel)
   die niets deelt met "leg een record aan als dit gebeurt".
3. **Het kanaal is bewust laagdrempelig.** ntfy op een geheim topic vraagt geen account, geen
   koppeling en geen aflevergarantie. Precies goed voor "de beheerder krijgt een piep", en
   precies verkeerd als vervanger voor een postvak met leesstatus.

**Wat het nieuwe systeem er wel van moet erven, en dat is niet niets:**

- **Het ontdubbelmodel.** `signature()` (`log_watcher.py:312`) normaliseert een melding tot
  een stabiele sleutel door tijdstempels, IP-adressen, gekoppelde identifiers en losse getallen
  weg te strippen, tot maximaal 120 tekens. Daar bovenop een venster in uren. Dat is precies
  het model dat "twintig herstarts is één melding" oplost, het is uitgeschreven, en het is in
  productie beproefd.
- **En de fout die erin zit.** De toestand van de ingebouwde planner is een dict in het
  geheugen, dus na een herstart begint de ontdubbeling opnieuw. Het commentaar noemt dat "at
  worst repeats one alert", en voor ntfy is dat waar. Voor een postvak is het dat niet: dan
  krijgt iedereen na elke uitrol dezelfde meldingen opnieuw. **De ontdubbelstaat van het
  nieuwe systeem hoort in Postgres, niet in het geheugen.**

**Ook meenemen**: ntfy staat standaard op `ntfy.sh`, dus buiten onze deur. Op productie kan
dat: de namespace `rig-prd-operations` draagt
`egress.projectcalico.org/egressGatewayPolicy: "internet"`
(`bootstrap/rig-system/kustomize/overlays/odcn-production/namespace.yaml:7`) en het
netwerkbeleid van OPI laat uitgaand verkeer op 443 naar elke bestemming toe
(`bootstrap/rig-system/kustomize/operations-manager/overlays/odcn-production/network-policy.yaml`).
Dat is relevant voor deel 3, want het bepaalt ook wat er met Mattermost kan.

---

## 10. Wat ik verder tegenkwam

De acht bronnen uit de opdracht zijn een startpunt en geen afbakening. Dit kwam er nog uit:

| event | bron | onderwerp | belanghebbenden | ernst | standaardkanaal |
|---|---|---|---|---|---|
| Een project heeft toegang tot een ander project gekregen of verloren | `opi/services/catalog/cross_domain_access/` (dienst `cross-domain-access`) | project (twee projecten tegelijk) | beheerders van BEIDE projecten | actie nodig | postvak + mail |
| Een bijlage is toegevoegd, gewijzigd of verwijderd | de taken `configure_attachment` en `delete_attachment`; dienst `opi/services/catalog/attachments/` | deployment | projectbeheerder | ter informatie | postvak |
| Een geheim of sleutel is geroteerd | **bestaat nog niet als gebeurtenis**: rotatie gebeurt bij het opnieuw verwerken van een project | project | projectbeheerder | ter informatie | postvak |
| Een dienst is aan een project toegevoegd of eruit gehaald | de taken `add_service` en de dienstverwijdering (`handle_service_removal` per dienst) | project | projectbeheerder | ter informatie | postvak |
| De bootstrap in git wijkt af van wat er draait | **bestaat nog niet**: punt 11 van `plans/mail-vervolgpunten.md` beschrijft het probleem (een commit in `bootstrap/rig-system/.../odcn-production` is pas een wijziging als iemand `task bootstrap-argo-system` draait) en stelt een detectie voor | platform | platformbeheerder | storing | ntfy |
| Een clonebewerking heeft een nieuwe generatie gemaakt | `opi/manager/revision_manager.py`, via `opi/services/catalog/shared/revisions.py` | deployment | projectbeheerder | ter informatie | geen |

**`cross-domain-access` verdient de aandacht die het niet krijgt in de opdracht.** Het is de
enige gebeurtenis in de hele inventarisatie waarbij de belanghebbende in een ANDER project
zit dan waar de gebeurtenis ontstaat. Dat breekt de aanname "je ziet meldingen van je eigen
projecten" die alle andere regels stilzwijgend maken, en het is precies het soort geval dat
een bevragingsmodel (richting B in deel 2) lastig maakt: de autorisatieregel is niet
"is deze persoon lid van het project van de gebeurtenis" maar "is deze persoon lid van een van
de twee projecten die de gebeurtenis noemt".

---

## De groepering naar type

Dit is de knop waar iemand straks per stuk aan draait. Het aantal is een echt ontwerpbesluit:
te veel typen geeft een instellingenscherm dat niemand doorloopt, te weinig geeft een
aan-uitknop die niemand gebruikt.

**Voorstel: twaalf typen.** Dat is de uitkomst van drie regels, in deze volgorde:

1. **Eén type per beslissing die een redelijk mens anders zou nemen.** Als niemand denkbaar
   is die A wel wil en B niet, horen A en B in één type. "Backup mislukt" en "herstel mislukt"
   zitten daarom samen; "mijn eigen taak is klaar" en "er is iets met mijn project gebeurd"
   niet.
2. **Nooit meer typen dan er meldingen zijn.** Een type dat drie keer per jaar vuurt is een
   regel in een scherm die 362 dagen niets doet. Die gaat bij een buur in.
3. **De ernst is geen type.** Ernst is een eigenschap van de melding, en die verschilt binnen
   een type (een uitrol kan slagen of falen). Wie alleen storingen wil, zet een filter op
   ernst en niet twaalf knoppen om.

| # | Type (voorstel) | Wat erin zit | Standaard voor projectbeheerder | Standaard voor projectlid |
|---|---|---|---|---|
| 1 | `uitrol` | de uitrolgroep uit paragraaf 1, plus ArgoCD-renderfouten | postvak + mail bij mislukking | postvak |
| 2 | `verwijdering` | de verwijdergroep uit paragraaf 1 | postvak + mail | postvak |
| 3 | `gezondheid` | OOM, image-pull, crashlus, probe-kill, uitgeschakelde component | postvak + mail | postvak |
| 4 | `platform-ingreep` | automatische stemmer, slaapstand, weesopruiming | postvak + mail | geen |
| 5 | `gegevens` | backup, herstel, kloon, schemabeheer, bewaartermijn | postvak + mail bij mislukking | geen |
| 6 | `aanvraag-ingediend` | een goedkeuring wacht op MIJ | postvak + mail | n.v.t. |
| 7 | `aanvraag-besloten` | mijn aanvraag is goedgekeurd of afgewezen | postvak + mail | postvak |
| 8 | `leden-en-toegang` | leden, rollen, uitnodigingen ingewisseld, cross-domain-access | postvak + mail | postvak |
| 9 | `dienstwijziging` | dienst toegevoegd of verwijderd, dienstconfiguratie, bijlagen | postvak | geen |
| 10 | `werkomgeving` | databaseconsole, ad-hoc jobs | postvak | geen |
| 11 | `platform-mededeling` | release, onderhoud, clusterbrede berichten | postvak + mail | postvak + mail |
| 12 | `beheer` | gebruikersbeheer, wezen, drift, scanbevindingen | n.v.t. | n.v.t. |

Type 12 is alleen zichtbaar voor platformbeheerders; type 6 alleen voor wie beoordeelt. Voor
de andere tien geldt de gewone regel: je ziet wat er met jouw projecten gebeurt.

**Waarom niet minder.** Vier of vijf typen ("uitrol, gezondheid, aanvragen, beheer") leest
prettiger maar levert een scherm op waar de enige zinnige handeling is om alles aan te laten.
De typen 4 en 5 zijn precies de twee waarvan de opdrachtgever zegt dat de eigenaar ze achteraf
moet weten, en die moet je apart kunnen aanzetten zonder ook elke geslaagde uitrol binnen te
krijgen.

**Waarom niet meer.** Per taaksoort (23), per dienst (23 diensttypen in `ServiceType`,
`opi/services/services_enums.py:4`) of per `failure_type` levert honderden knoppen op.
Ergens moet de grens liggen en dit is een verdedigbare plek. Wie meer verfijning wil, krijgt
hem in de melding zelf (die draagt de taaksoort en de dienst) en niet in het
instellingenscherm.

**De open beslissing.** Twaalf is mijn voorstel, geen wet. Wat de opdrachtgever hier moet
beslissen is niet het getal maar de regel eronder: **draait iemand per type aan één knop
(aan/uit), of per type per kanaal (postvak / mail / Mattermost)?** Het tweede is wat GitHub
doet en wat de wens beschrijft; het is ook drie keer zo veel scherm. Mijn aanbeveling: per
type per kanaal, maar met werkbare standaarden per rol zodat niemand het scherm hoeft te
openen om iets zinnigs te krijgen. Zie deel 3.

## Wat hier bewust niet in staat

- **Metrieken en drempelwaarden.** "CPU boven 80 procent" is bewaking en geen melding over
  een gebeurtenis. Prometheus en Grafana doen dat al en hebben er hun eigen alarmering voor.
- **Applicatielogs van de klant.** Wat er in de container van een project gebeurt is van dat
  project. ZAD meldt over het platform en over de deployment, niet over de applicatie.
- **De inhoud van de meldingsteksten.** Wat er precies staat is werk voor de bouwfase, met
  één regel die nu al vastligt: de gebruiker wordt aangeschreven met "je".
