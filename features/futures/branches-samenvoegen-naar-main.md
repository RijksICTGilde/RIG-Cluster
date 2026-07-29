# Branches samenvoegen naar main

**Status**: Plan, nog niet uitgevoerd
**Doel**: al het werk uit de losse branches op één integratiebranch krijgen, gecontroleerd,
zonder dat er iets verloren gaat, zodat die daarna in één keer naar main kan.

`main` in deze repo is leidend. De stand bij het schrijven van dit plan is `51fd763e`, en die is
gepusht naar forgejo. GitHub (`origin/main`) loopt 21 commits achter en is buiten scope van dit
plan.

## De feitelijke structuur

Gemeten, niet aangenomen. Alle vijf de actieve branches bevatten de huidige main volledig (nul
commits achter), en ze delen één gemeenschappelijke basis.

```
main 51fd763e
 └── uniform-declarative-platform-services  2f2c7c5d  (+80)   27-07 19:09
      ├── pvc-marked-for-deletion-...       9630cf80  (+16)   27-07 19:53
      ├── health-check-service              b9191910  (+3)    28-07 22:46
      └── (gedeeld tot 537b00a9)
           ├── implementatieplan-sleep-mode fbc3315d  (+1)    28-07 22:23
           └── services-leespaden-...       bb5d276d  (+29)   28-07 22:34
```

Twee dingen die hieruit volgen en die de volgorde bepalen:

1. **`uniform-declarative-platform-services` is de gedeelde basis van alles.** Die moet als
   eerste, anders sleept elke andere merge die 80 commits alsnog mee en wordt elk conflict
   dubbel beoordeeld.
2. **`implementatieplan-sleep-mode` en `services-leespaden` delen hun geschiedenis tot en met
   `537b00a9`.** Merge je sleep-mode eerst, dan komt leespaden daarna vrijwel schoon binnen.
   Andersom ook, maar dan is de volgorde niet chronologisch en wordt de historie lastiger te
   lezen.

## Volgorde

Op afrondingstijdstip (committer-tijd van de tip), met de afhankelijkheid voorop:

| # | Branch | Uniek | Afgerond |
|---|---|---|---|
| 1 | `uniform-declarative-platform-services` | 80 | 27-07 19:09 |
| 2 | `pvc-marked-for-deletion-blokkeert-de-render-en-nie` | 16 | 27-07 19:53 |
| 3 | `implementatieplan-sleep-mode-slaapstand-met-wekken` | 23 | 28-07 22:23 |
| 4 | `services-leespaden-presentatie-en-formulierintegri` | 29 | 28-07 22:34 |
| 5 | `health-check-service` | 3 | 28-07 22:46 |

## Waar de conflicten zitten

Verwacht die vooral tussen 4 en 5. `services-leespaden` bevat een systematische veegactie van
alle lezers van een `services`-lijst plus een nieuwe `detail_page_sections`-hook, terwijl
`health-check-service` een health-check-service toevoegt en het oude `probe`-blok uitfaseert.
Beide zitten in dezelfde bestanden en `health-check-service` is niet gebouwd op de veegactie.

De rest is naar verwachting rustig: 1 is de basis, 2 raakt vooral dashboard en ArgoCD-status, en
3 en 4 delen hun geschiedenis.

## Aanpak

Een integratiebranch vanaf main, waarop de vijf in bovenstaande volgorde worden samengevoegd.
Merge, geen rebase: bij vijf branches met 80 tot 109 commits levert rebasen dezelfde conflicten
meerdere keren op, en de historie van de losse branches blijft leesbaar.

```
git switch -c integratie-main main
git merge --no-ff forgejo/uniform-declarative-platform-services
...
```

Per stap, en dit is het punt van dit plan:

1. **Merge één branch.**
2. **Los conflicten op met de bedoeling van beide kanten voor ogen**, niet door één kant te
   kiezen. Bij twijfel: welke van de twee commits is jonger, en welke bevat een test die de
   ander niet heeft.
3. **Draai de testsuite** en `ruff`/`pyright`.
   → verify: geen nieuwe fouten ten opzichte van de vorige stap. Bekende pre-existing fouten:
   `tests/test_subdomain_connector.py`, `tests/test_runs_service.py` en
   `tests/test_user_admin_service.py` hebben importfouten, en `tests/test_sleep_mode_manifests.py`
   heeft één testvolgorde-afhankelijke fout die los draaiend slaagt.
4. **Commit de merge pas als het groen is.** Een merge die rood staat gaat terug, hij gaat niet
   door naar de volgende stap.

## Controle dat er niets verloren gaat

Dit is waar dit plan om draait, dus het moet gemeten worden en niet aangenomen.

Na elke merge:

```
git rev-list --count <branch>..HEAD   # moet 0 zijn: alles van die branch zit erin
git log --oneline HEAD..<branch>      # moet leeg zijn
```

En aan het eind, voor alle vijf tegelijk. Blijft er iets over, dan is er tijdens een
conflictoplossing werk weggevallen en moet dat commit voor commit worden nagelopen.

Aanvullend, want een commit kan aanwezig zijn terwijl zijn inhoud is weggemergd:

- Vergelijk het aantal tests vóór en na. De suite hoorde na de laatste stap **meer** tests te
  hebben dan elke afzonderlijke branch, niet minder.
- Loop de testbestanden na die in meerdere branches zijn aangeraakt en controleer dat de tests
  van beide kanten er nog in staan.

## Volledige controle na de laatste merge

De per-stap-controle hierboven houdt de reeks gezond, maar zegt niets over het geheel. Als alle
vijf erin zitten, volgt een volledige verificatie. Pas daarna is de branch aanbiedbaar.

**1. De hele unit-suite.**
→ verify: `uv run pytest tests/ -q` levert geen andere fouten op dan de bekende pre-existing
importfouten. Noteer het aantal geslaagde tests; dat hoort hoger te liggen dan bij elke
afzonderlijke branch.

**2. De standalone E2E-suite**, die geen sandbox nodig heeft.
→ verify: `uv run pytest -m e2e` groen. Deze vangt onder meer htmx-fouten in wizardstappen die
een unit-test niet ziet.

**3. De sandbox, echt uitgerold.** Dit is de enige stap die aantoont dat de samengevoegde
wijzigingen ook samen werken in plaats van alleen samen compileren.

De sandbox is gedeeld, dus eerst claimen en na afloop vrijgeven:

```
orch sandbox claim
...
orch sandbox release
```

Rol de integratiebranch daadwerkelijk uit op de sandbox, dus niet alleen de tests draaien tegen
een bestaande omgeving. De OPI van deze branch moet er draaien, want een deel van wat er is
samengevoegd zit in verwerkingscode die je alleen ziet als hij echt draait.

```
task sandbox:update-operations-manager
```

Draai daarna de sandbox-gebonden E2E-tests (onder meer de sleep-mode-suites die op deze branches
zijn toegevoegd) en maak minstens één project aan via de wizard, met een component dat services
draagt en een component dat er geen heeft. Dat tweede is precies de vorm die gisteren zes
verschillende bugs blootlegde, en geen van die zes was zichtbaar in een unit-test.

→ verify: het project komt daadwerkelijk draaiend omhoog (pods `Running`, ArgoCD-applicatie
gezond), niet alleen "opgeslagen zonder fout".
→ verify: de OPI-logs bevatten geen `ERROR` of stacktrace tijdens de verwerking.

**Let op een bekende zwakte in deze suite**: sommige E2E-wizardtests controleren alleen de
bestandstoestand en bleven daardoor groen op een volledig kapotte create. Er bestaat een
`wait_for_task()` die de taakuitkomst afwacht. Gebruik die, of stel expliciet vast dat de test die
je afvinkt daadwerkelijk de uitkomst controleert en niet alleen dat er iets is weggeschreven.

## Losse gevallen

**`projectstore-fast-safe-backend-agnostic-project-fi`** staat 40 vooruit maar ook 29 achter op
main. Die is dus niet op de huidige main gebaseerd en hoort niet in dezelfde reeks thuis. Eerst
vaststellen of dat werk al via een andere weg op main is beland; zo niet, apart behandelen.

**`services-audit-en-herstelplan`** (0 vooruit) is de branch van de afgewezen taak RC-8 en bevat
niets. Kan weg.

**De oude branches blijven buiten scope.** `forgejo`, `zad-task-details`, `om-1`, `om-2`,
`rig-8`, `rig-9`, `rig-10`, `sandbox-dev-server-fixes`, `test-create-project-wizard-...` en de
`claude/*`-branches lopen honderden commits achter en zijn afgerond of verlaten. Die worden
bewust genegeerd: niet nalopen, niet mergen, niet verwijderen.

## Wat er níet gebeurt

De integratiebranch wordt **niet** zelf naar main gemerged. Dat is een aparte beslissing van de
gebruiker, nadat de branch groen is en de controle op volledigheid is gedaan.
