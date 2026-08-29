# OOM auto-tune escaleert onbegrensd tot het clusterplafond

## Wat er gebeurde

Op 24 augustus liep de geheugenlimiet van `asses-k2n/pr-494`, component `api`, tussen 10:14 en 10:19 in negen stappen van 45Mi naar het clusterplafond van 4096Mi:

```
45 -> 175 -> 350 -> 525 -> 788 -> 1182 -> 1773 -> 2660 -> 3990 -> 4096Mi
```

Bij het plafond kon de tuner niet verder en faalde de deploytaak op:

```
asses-k2n-pr-494: OOM detected for api in pr-494 but auto-tune could not determine new limits
```

Dit is niet dezelfde fout als die van 21 augustus (`tests/test_oom_tune_zonder_metrics.py`, destijds `pr-469`). Die fix zorgde dat een door de watcher gemelde OOM ook zonder meetdata tot een verhoging leidt, en die werkt: hij is zichtbaar op elke trede hierboven als `No memory data for pr-494-api but OOM kills detected, using current limits (...) as baseline`. Wat ontbreekt is de begrenzing eromheen. Zelfde foutmelding, tegenovergestelde oorzaak: toen gaf de tuner te vroeg op, nu gaat hij te lang door.

## Grondoorzaak

Er zijn drie samenwerkende gebreken, alle in `operations-manager/python/opi/services/oom_watcher.py` tenzij anders vermeld.

**1. De pogingenteller reset per ronde in plaats van per deployment.** Er zijn twee losse tellers voor hetzelfde doel. Het inline pad (`create_health_check_callback`, tijdens de sync-wacht) gebruikt de moduledict `_inline_oom_attempts`. Het fire-and-forget pad (`schedule_oom_check` naar `_run_oom_check`) gebruikt een `attempt`-parameter die binnen een keten netjes wordt doorgegeven. Beide beginnen per deploytaak opnieuw. Omdat elke geslaagde tune via `_queue_refresh_task` een `refresh_deployment`-taak inschiet, en die taak in `task_handlers_operations.py` rond regel 376 een verse `schedule_oom_check` start met `attempt=1`, reset de rem zichzelf bij elke ronde van de lus die hij moet stoppen. In de productielogs is dat direct zichtbaar: taak `d3f64366` blokkeert correct op `3/3` om 10:18:39, waarna taak `be3eb839` om 10:19:51 weer op `1/3` staat.

Bovenop de reset vertakt het: taak `ef95973b` spawnde drie refresh-taken, `f64dac10` nog een, en er kwamen twee externe upserts overheen. Zes taken werkten tegelijk aan hetzelfde deployment, elk met een eigen teller.

**2. De inline rem is een snapshot.** In `create_health_check_callback` worden `current_attempts` en `oom_budget_exhausted` eenmalig gelezen bij het bouwen van de callback en daarna in de closure gebruikt. Binnen een callback kan `oom_budget_exhausted` daardoor nooit van `False` naar `True` kantelen, en de ophoging `_inline_oom_attempts[attempt_key] = current_attempts + 1` schrijft altijd hetzelfde getal. Twee gelijktijdige callbacks op hetzelfde deployment zien elkaars ophogingen niet.

**3. De escalatie meet nooit het effect van haar eigen ingreep.** Alle twaalf OOM-detecties in het venster betroffen dezelfde pod, `pr-494-api-fb654fcc5-rcf6g`, dus een enkele ReplicaSet die nooit werd vervangen. De eerste detectie viel om 10:14:13 terwijl ArgoCD nog `OutOfSync, health=Progressing` was. Bij elke volgende ronde brak de health-error de sync-wacht af voordat de vorige verhoging was uitgerold, waarna de watcher dezelfde ongewijzigde pod opnieuw als vers bewijs las. Het bestaande `superseded generation`-filter ving dit niet af, omdat die pod op dat moment nog de huidige generatie was.

Daarnaast is het enige bovenste plafond `get_max_memory_limit_mi` uit `cluster_config.py`, wat op `odcn-production` 4096Mi is. Er is geen begrenzing die de verhouding tot de oorspronkelijk verklaarde limiet bewaakt, dus een component die op 45Mi begon mag ongehinderd naar 4Gi.

## Taken

### Taak 1: een gedeelde teller per deployment, die niet reset op een geautomatiseerde refresh

Bestanden: `opi/services/oom_watcher.py`, `opi/core/task_handlers_operations.py`, `opi/core/task_handlers_project.py`.

Vervang de twee losse mechanismen door een moduledict die beide paden delen, met de sleutel `"{project}/{deployment}"`. Het fire-and-forget pad moet zijn gate op die dict baseren in plaats van op de doorgegeven `attempt`-parameter. De parameter mag blijven bestaan voor de logregels, maar mag niet langer bepalen of er nog getuned mag worden.

Verwijder de onvoorwaardelijke reset onderaan `create_health_check_callback` (nu regel 945 en 946, `_inline_oom_attempts.pop(attempt_key, None)` met de opmerking dat een verse deploy schoon begint). Reset voortaan alleen expliciet, via de bestaande publieke functie `reset_inline_oom_attempts`, en alleen wanneer het om een echte nieuwe deploy gaat. `_queue_refresh_task` zet al `automated_remediation: True` in de payload; de refresh-handler moet die vlag gebruiken en de teller juist niet resetten wanneer hij gezet is. Een gebruikersactie of een image-bump moet wel resetten.

Assertie: een test die een volledige ronde nabootst, dus watcher detecteert OOM, tune commit, `_queue_refresh_task` met `automated_remediation: True`, en daarna de nieuwe `schedule_oom_check` die de refresh-handler start. De teller moet over die rondes doorlopen van 1 naar 2 naar 3, en de vierde ronde mag niet meer tunen. Tegen de huidige code faalt die test, omdat de teller op 1 blijft staan.

Tweede assertie: na een reset die hoort bij een echte nieuwe deploy mag er weer getuned worden. De bestaande test `test_fresh_deploy_resets_oom_budget` dekt dit al en moet groen blijven.

### Taak 2: de inline rem live lezen in plaats van snapshotten

Bestand: `opi/services/oom_watcher.py`, functie `create_health_check_callback`.

Lees `current_attempts` en de afgeleide `oom_budget_exhausted` binnen `_callback` bij elke aanroep uit de gedeelde dict, in plaats van eenmalig bij het bouwen. De waarschuwing die nu bij het bouwen wordt gelogd moet meeverhuizen naar het moment waarop de rem daadwerkelijk dichtklapt, anders verdwijnt hij uit de logs.

Let op dat de callback ook na uitputting moet blijven bestaan en `image_pull` en `crash_loop` moet blijven melden. Dat gedrag is nu expliciet vastgelegd in `test_image_pull_still_detected_when_oom_budget_exhausted` en `test_crash_loop_still_detected_when_oom_budget_exhausted`, en die moeten groen blijven.

Assertie: een test die twee callbacks voor hetzelfde deployment bouwt voordat er iets is gedetecteerd, en de OOM-detecties over beide verdeelt. Na drie detecties in totaal moet de vierde, op welke van de twee callbacks dan ook, geen OOM-failure meer opleveren. Tegen de huidige code faalt dat, omdat beide callbacks hun eigen snapshot van nul dragen.

### Taak 3: absolute bovengrens ten opzichte van de verklaarde limiet

Bestanden: `opi/services/resource_tuning_service.py`, `opi/core/config.py`.

Begrens de auto-tune tot een veelvoud van de in de catalogus verklaarde root-limiet. `resource_tuning_service.py` leest die root al rond regel 386 tot 399 als `root_resources["limits_memory"]`, waar hij nu alleen als ondergrens dient. Gebruik hem ook als bovengrens voor het automatische pad.

Naamvoorstel, nog niet vastgelegd: instelling `OOM_MAX_GROWTH_FACTOR` met standaardwaarde 8. Naam en waarde zijn een voorstel, kies gerust iets beters en meld dat in de PR. Met factor 8 was `pr-494` op 360Mi gestopt in plaats van op 4096Mi.

Boven de grens mag er geen verhoging meer plaatsvinden. De melding die dan volgt moet expliciet om handmatig ingrijpen vragen en de verhouding noemen, dus de verklaarde limiet, de huidige limiet en de factor. De huidige tekst `auto-tune could not determine new limits` is hier misleidend, want er is wel degelijk een limiet bepaald, hij is alleen geweigerd. Die tekst staat in `opi/services/catalog/resource_tuning/__init__.py` rond regel 75 en mag voor dit geval een eigen, duidelijker variant krijgen.

Assertie: een test met een root-limiet van 45Mi en een huidige limiet op de grens, die aantoont dat er geen verhoging meer uit komt en dat de foutmelding naar handmatig ingrijpen wijst. Plus een negatieve controle: ruim onder de grens tunet hij gewoon door.

Let op dat de bestaande fallback uit `tests/test_oom_tune_zonder_metrics.py` intact blijft. Een gemelde OOM zonder meetdata moet nog steeds verhogen zolang de bovengrens niet is bereikt. Die test moet groen blijven.

### Taak 4: niet opnieuw tunen zolang de vorige verhoging niet is uitgerold

Bestand: `opi/services/oom_watcher.py`.

Leg bij een tune vast op welke pod-template-hash de OOM werd waargenomen. Bij een volgende detectie geldt: is de hash gelijk aan de vorige, dan draait de oude pod nog en is de vorige verhoging dus niet uitgerold. In dat geval niet opnieuw tunen, maar wachten op een nieuwe generatie. De helper `_get_current_pod_template_hash` bestaat al, op regel 156.

Dit is het vangnet dat de escalatie in het incident had gestopt, ook zonder de tellerfixes: alle twaalf detecties zagen dezelfde hash.

Denk aan het geval waarin de hash niet te bepalen is. `_get_current_pod_template_hash` geeft dan `None` terug en de bestaande code valt terug op het evalueren van alle pods. Kies hier bewust: bij een onbekende hash niet blokkeren, want anders valt de auto-tune stil zodra kubectl hapert. Documenteer die keuze in de code.

Assertie: een test met twee opeenvolgende OOM-detecties op dezelfde pod-template-hash die aantoont dat er precies een tune volgt. Plus een tweede test die na een gewijzigde hash wel een tweede tune toestaat. Plus een derde die aantoont dat een onbekende hash de tune niet blokkeert.

### Taak 5: inventarisatiescript voor de restanten, plus een notitie in TODO.md

Bestanden: `scripts/`, `Taskfile.yaml`, `TODO.md`.

Een read-only rapportage die per project de huidige limiet van elk component vergelijkt met de root-catalogus-limiet en met de `oom-watcher`-entries in `resources.history`, en de gevallen lijst waar de verhouding buiten de nieuwe bovengrens uit taak 3 valt. Alleen rapporteren, niets wijzigen. Ontsluit het via een task in `Taskfile.yaml`, conform de projectvoorkeur om Taskfile te gebruiken in plaats van losse shellscripts.

Het script hoeft niet tegen productie te draaien in deze taak; dat doen wij zelf na de merge. Een test tegen een verzonnen projectdict volstaat.

Een scan van productie op dit moment laat deze verdachte waarden zien. De niet-ronde getallen dragen de kenmerkende ladder van herhaalde vermenigvuldiging, ronde waarden zoals 1536Mi en 1Gi zijn vrijwel zeker handmatig gezet:

```
4039Mi  rig-prd-mpfm-w3h/pr-137-clickhouse
3831Mi  rig-prd-openp-4pw/poc-applicatie
3831Mi  rig-prd-mpfb-8wh/pr-150-clickhouse
2163Mi  rig-prd-regel-k4c/regelrecht-editor
1445Mi  rig-prd-mpfm-w3h/pr-191-magazijnb
1445Mi  rig-prd-mpfm-w3h/pr-189-magazijnb
1442Mi  rig-prd-regel-k4c/upload-editor
1418Mi  rig-prd-mpfpsm-lcl/pr-195-profiel
1165Mi  rig-prd-algor-odc/deployment-1-component-1
```

Zet in `TODO.md` een punt dat op zichzelf leesbaar is: wat er aan de hand is, waar het zit, wat het voorstel is en welke beslissing nog open staat, namelijk of de opgeblazen limieten automatisch teruggezet mogen worden of alleen gerapporteerd.

## Bestaande tests die moeten meeveranderen

Twee tests in `tests/test_oom_watcher.py` leggen het huidige, gebrekkige gedrag vast als gewenst en kunnen niet ongewijzigd blijven:

- `test_resets_counter_on_creation` op regel 822 stelt de reset bij het bouwen van de callback vast als correct gedrag. Dat is precies gebrek 1. Deze test moet omgedraaid worden: bouwen mag de teller juist niet meer wissen.
- `test_oom_cap_bounds_tuning_within_a_deploy` op regel 743 leunt in zijn opzet en commentaar expliciet op de "fresh-deploy pop" en verwacht dat de sleutel na uitputting verdwijnt. Herzie hem zo dat hij de nieuwe semantiek vastlegt, dus een teller die over rondes doorloopt.

Werk in beide gevallen ook de docstring bij. Die documenteert nu het foutieve model.

## Validatie

```
cd operations-manager/python
uv run pytest tests/test_oom_watcher.py tests/test_oom_tune_zonder_metrics.py -x -q --tb=short
uv run pytest tests/ -q
uv run ruff check . --fix
uv run ruff format .
uv run pyright
```

Alle nieuwe tests moeten aantoonbaar falen tegen de huidige code voordat de fix erin gaat. Noem in de PR per taak welke test dat is en wat hij aantoont.

## Randvoorwaarden

- Niet uitrollen naar productie. De sandbox mag, prod niet.
- Geen verwijzing naar Claude, Anthropic of AI in commitberichten of PR-tekst.
- Commitberichten in het Nederlands, in lijn met de bestaande historie van deze repo.
- Houd de wijzigingen chirurgisch. Raak geen aangrenzende code aan die niet uit deze vijf taken volgt.
- De vijf taken zijn onafhankelijk te reviewen. Splits ze in aparte commits.
