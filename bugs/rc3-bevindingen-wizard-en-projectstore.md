---
title: RC-3 Bevindingen — Wizard-fouten, ProjectStore-risico's en procesanalyse
status: findings
created: 2026-07-20
tags: [rc-3, projectstore, wizard, e2e, argocd, review]
branch: projectstore-fast-safe-backend-agnostic-project-fi
pr: 6
base-commit: 5c72cef51b9e3bbee5e096f192b00d99d8587261
---

# RC-3 Bevindingen

Overdrachtsdocument voor een volgende Claude-sessie. Alles hieronder is geverifieerd op een
draaiende sandbox-cluster tenzij expliciet anders vermeld. Elk probleem heeft een concrete
locatie en een voorgestelde fix.

**Belangrijk onderscheid:** de wizard-fouten (Deel A) en het ArgoCD-probleem (Deel B) zijn
**pre-existing** — ze staan los van RC-3. Bewezen via een gecontroleerde A/B: dezelfde 3 tests
falen identiek op `rc-2` (vóór de branch) en op de branch. Deel C is wél RC-3-specifiek.

---

## Hoe je dit reproduceert (belangrijk — dit was eerder de blokkade)

De vorige sessie stopte met "geen kubeconfig". Dat klopte niet: de dockersocket is gemount,
dus dit werkt gewoon:

```bash
kind get clusters                                    # -> rig-sandbox
kind get kubeconfig --name rig-sandbox > /tmp/kubeconfig
export KUBECONFIG=/tmp/kubeconfig
kubectl -n rig-system get pods
```

`task sandbox:update-operations-manager` werkt **niet** in de container: `kustomize` en `sops`
ontbreken en `security/sandbox-key.txt` bestaat niet. Dat is ook niet nodig — de env-secrets
staan al in de cluster. Omdat deze branch alléén Python-source wijzigt (geen `pyproject.toml`,
`uv.lock` of `Dockerfile`), volstaat dit en is het omkeerbaar:

```bash
# Dockerfile.thin:
#   FROM operations-manager:rc-2
#   COPY operations-manager/python/opi /app/opi
#   COPY operations-manager/python/manifests /app/manifests
#   COPY operations-manager/python/alembic.ini /app/alembic.ini
docker build -f Dockerfile.thin -t operations-manager:rc-3 .
kind load docker-image operations-manager:rc-3 --name rig-sandbox
kubectl -n rig-system set image deployment/operations-manager operations-manager=operations-manager:rc-3
# terugrollen: set image ... operations-manager:rc-2   (rc-2 staat nog op de node)
```

E2E draaien (let op: `--timeout` uit het Taskfile werkt niet, `pytest-timeout` ontbreekt):

```bash
cd operations-manager/python
uv run playwright install chromium
E2E_BASE_URL=https://zad.sandbox.rijksapp.dev \
E2E_SECRET_KEY=sandbox-dev-secret-key-fixed-for-stable-sessions-32min \
FORGEJO_URL=https://forgejo.sandbox.rijksapp.dev \
FORGEJO_USER=rig-admin FORGEJO_PASSWORD=admin1234 \
E2E_TRACE=1 uv run pytest tests/e2e/ -m "e2e and sandbox" -v
```

---

## Deel A — Wizard: 2 falende e2e-tests (PRE-EXISTING)

```
FAILED tests/e2e/test_wizard_create.py::test_wizard_minimal_project
FAILED tests/e2e/test_wizard_create.py::test_wizard_project_appears_in_list
```

### Oorzaak: de test loopt niet in de pas met de wizard — NIET een wizardbug

Dit is belangrijk, want het scheelt veel zoekwerk: **de wizard zelf werkt correct.**

Bewijs uit de faalscreenshots (`tests/e2e/artifacts/FAILED-*.png`):

- De wizard staat op **stap 6 "Webadres"**.
- Stappen **1 t/m 5 zijn groen** (voltooid), stap 6 is blauw (actief).
- Er is **geen enkele foutmelding** zichtbaar.
- De gegenereerde URL toont `web-productie-...sandbox.rijksapp.dev` — de component uit stap 4
  is dus correct doorgekomen.
- De knop op stap 6 heet **"Controleren"**, niet "Volgende".

Een eerder vermoeden was dat een veldvalidatie of een niet-aangevinkte checkbox de wizard
blokkeerde. Dat is hiermee uitgesloten: bij een blokkade was de wizard op die eerdere stap
blijven staan en was die stap nog blauw geweest, niet groen.

De wizard heeft **6 stappen** (`opi/templates/wizard/wizard_start.html.j2:71` zegt letterlijk
*"6. Controleren en aanmaken"*):

| # | Stap |
|---|------|
| 1 | Projectgegevens |
| 2 | Services |
| 3 | Projectleden |
| 4 | Componenten |
| 5 | Deployment |
| 6 | Webadres |

`tests/e2e/test_wizard_create.py` doet exact **5** `click_next()`-calls en verwacht daarna de
reviewpagina. Het landt dus op stap 6 en bereikt review nooit. De testcommentaren zijn ook
verouderd ("Step 5: Domains" — stap 5 is in werkelijkheid Deployment).

Dat verklaart beide fouten:
- `test_wizard_minimal_project` — assert `project_name in page_text` faalt, want stap 6 toont
  de projectnaam niet.
- `test_wizard_project_appears_in_list` — `Locator.fill` timeout op `input[type='text']`, want
  stap 6 heeft alléén twee dropdowns (Basisdomein, URL-formaat) en geen tekstveld.

### Voorgestelde fix

In `tests/e2e/test_wizard_create.py`: voeg de ontbrekende stap toe (Deployment) en corrigeer de
commentaren, zodat er 6 keer wordt doorgeklikt vóór de review-assert. Doe hetzelfde voor
`test_wizard_project_appears_in_list`.

**Robuuster alternatief (aanrader):** laat `WizardHelper` niet op een vast aantal kliks
vertrouwen maar doorklikken tot de reviewstap bereikt is (bijv. tot de knop "Controleren" is
geweest, of tot de URL `/step/` verlaat). Dan breekt de test niet opnieuw zodra er een stap
bijkomt. Deze fout is precies test-rot: de wizard kreeg een stap erbij, de test is niet
meegegroeid.

### Nog te controleren
In de gegenereerde URL staat letterlijk `projectid` (`web-productie-projectid.sandbox...`).
Dat lijkt een placeholder omdat het project nog niet bestaat, maar verifieer of dit klopt of
dat de projectnaam hier hoort te staan.

---

## Deel B — ArgoCD: `user-applications` mist auto-sync (PRE-EXISTING)

```
FAILED tests/e2e/test_sandbox_flows.py::test_add_component_via_api
```

De task blijft hangen op *"Waiting for ArgoCD deployment sync"* tot de assert na 180s afgaat.
De subtasks die ertoe doen zijn wél `completed`: `Project creation`, `Component toevoegen`.
Het project en de component zijn dus correct weggeschreven — alleen ArgoCD synct nooit.

Oorzaak — de live Application mist het `automated`-blok:

```bash
kubectl -n rig-system get applications.argoproj.io user-applications -o jsonpath='{.spec.syncPolicy}'
# live:     {"retry":{...},"syncOptions":["CreateNamespace=false"]}
```

Terwijl de template `manifests/argocd-application.yaml.jinja:28-34` voorschrijft:

```yaml
syncPolicy:
  automated:
    prune: true
    selfHeal: true
    allowEmpty: false
  syncOptions:
    - CreateNamespace=false
```

`status.sync.revision` is leeg — de app heeft dus **nog nooit** gesynct en staat permanent
`OutOfSync` (health wel `Healthy`).

### Belangrijk vóór je "fixt"
Patch niet blind de live Application. Zoek eerst uit **waarom** het blok ontbreekt:

1. Maakt de provisioning-code de Application aan zónder `automated` (echte bug in de
   aanmaakcode/oudere template)? → fix de bron.
2. Of is het blok later weggehaald/gedrift? → dan volstaat herstellen.

Optie 1 is een echte bug die op elke nieuwe cluster terugkomt; alleen de live resource patchen
maskeert dat. `syncOptions` matcht wél met de template, dus de Application is waarschijnlijk
gemaakt vanuit een oudere versie of gedeeltelijk overschreven — dat pleit voor eerst
uitzoeken.

---

## Deel C — RC-3 blokkerende bug + ontwerpgaten (WÉL van deze branch)

### C1. BLOKKEREND — `oom_watcher.py:398` sloopt de warme werkkopie

```python
# opi/services/oom_watcher.py:381,398
project_data, filename, git_connector = await get_project_data_from_git(project_name)
...
finally:
    await git_connector.close()
```

Deze PR heeft het eigendomscontract van `get_project_data_from_git` omgedraaid maar slechts
één van de twee aanroepers aangepast:

- **Vóór:** functie maakte een eigen connector per call — *"The caller is responsible for
  closing"*. De `close()` was correct.
- **Nu:** `resource_tuning_service.py:114` retourneert `get_project_store().get_connector()`,
  de gedeelde warme kopie, en de docstring zegt expliciet dat aanroepers hem **niet** mogen
  sluiten.

`GitConnector.close()` doet **onvoorwaardelijk** `rmtree` van de working dir: bij een
meegegeven `working_dir` wordt `should_cleanup = False` gezet, maar die guard staat
uitgecommentarieerd op `git.py:1609-1610` (*"for now, we always remove the working directory
on close"*). Empirisch aangetoond:

```
should_cleanup: False (False = 'do not clean up')
warm dir exists BEFORE close: True
warm dir exists AFTER  close: False
```

**Onherstelbaar zonder herstart:** de store cachet `self._connector` zonder liveness-check en
`ensure_repo_cloned()` slaat over op `self._repo_cloned` (`git.py:573`), die `True` blijft.
Niets kijkt naar `_closed`. Elke volgende project-file-operatie draait daarna tegen een
verwijderde `cwd`.

**Trigger is alledaags:** `disable_components_for_image_pull` wordt aangeroepen vanaf
`project_manager.py:2833` en `oom_watcher.py:481` — dus bij elke ImagePullBackOff (verkeerde
tag, ontbrekende registry-credential).

Het schendt bovendien de invariant die de PR zelf documenteert in
`features/project-store.md:103` ("Never close the warm connector").

**Fix:** verwijder `finally: await git_connector.close()` op `oom_watcher.py:398`.

**Test ontbreekt:** `disable_components_for_image_pull` wordt in tests altijd gemockt
(`test_oom_watcher.py:780`), daarom zien 87 groene oom/resource-tuning/startup-tests dit niet.
Voeg een regressietest toe.

**CI-guard schiet tekort:** `tests/test_single_path_enforcement.py` scant alleen op
`create_git_connector_for_project_files(`. Breid uit naar `.close()` op store-connectors —
die guard had dit gevangen.

### C2. Het slot dekt niet alle schrijvers

De docstring claimt dat writes geserialiseerd zijn door een per-repo `asyncio.Lock`, maar
`get_connector()` geeft dezelfde werkboom aan aanroepers die dat slot nooit nemen:

- `project_manager.py:4415` (`save_project_data()` + `commit_and_push`)
- `database_manager.py:2141`, `delete_project_manager.py:916,1015`, `minio_manager.py:2010`
- `resource_tuning_service.py:117` (`read_file_content` — ongelockte lees)

Ondertussen doet `reconcile()` mét het slot `reset --hard origin` + `git clean -fd`
(`git.py:874-879`). Een reconcile tussen een schrijfactie en zijn `commit_and_push` gooit dat
werk stilzwijgend weg. Daarnaast stageert `commit_and_push` met `git add -A` (`git.py:1173`),
wat op een gedeelde boom halfgeschreven bestanden van een andere schrijver kan meenemen.

Vóór deze PR had elke aanroeper een eigen tempdir; deze race bestond niet.

### C3. `mutate` levert de "nooit een blinde text-rebase"-garantie niet

`project_store.py:371` en de module-docstring beloven her-lezen + opnieuw toepassen +
hervalideren bij divergentie. Maar `push_changes` **rebaset intern** (`git.py:1429-1440`): bij
non-fast-forward roept hij `_rebase_on_remote` aan en bij succes wordt gewoon gepusht.
`GitPushConflictError` bereikt `mutate` alléén bij een *tekstueel* conflict.

Het normale geval — gelijktijdige bewerking in een ander deel van hetzelfde bestand — wordt dus
opgelost met precies de blinde tekstmerge die het ontwerp verbiedt, en gepusht **zonder
hervalidatie**. Het plan is hier expliciet over ("Never trust git text-rebase for correctness"
en "add post-rebase re-validation").

**Fix-richting:** push met `max_retries=1` of een niet-rebasende push-variant, zodat divergentie
altijd bij de re-apply-lus van `mutate` uitkomt.

### C4. Lager (maar wel oplossen)

- **`create()` / `save()` / `delete()` missen de rollback van `mutate`.** `_persist` gooit
  `GitPushConflictError` door vóór zijn reset-handler (`:477-483`), en `delete()` omzeilt
  `_persist` volledig (`:433-434`). Een mislukte push laat een niet-gepushte lokale commit
  achter; de eerstvolgende geslaagde `commit_and_push` van een *willekeurig ander* project
  sleept die commit alsnog mee naar de remote, onder een niet-gerelateerde commit-message.
- **`bootstrap()` heeft geen productie-aanroeper** — alleen `test_project_store.py:489,506`.
  Startup gebruikt nog `refresh_projects_from_git` / `replace_all_projects`
  (`startup.py:320,400`). De consolidatie uit het plan is dus niet echt aangesloten, en de
  docstring ("Starts from the remote state (reset --hard origin)") klopt niet: `bootstrap`
  roept `reset_to_remote` nooit aan.
- **`_refresh_cache` fallback** (`:493`) registreert `api_key=""` en `users=None`. Geen
  security-gat (lege keys worden afgewezen op `endpoint_util.py:35`, en lookup gaat op naam via
  `compare_digest`), maar het project wordt permanent niet-authenticeerbaar en verliest zijn
  member-allowlist tot een reconcile het repareert. Falen op de write is een beter faalgedrag.
- **Backend-agnostisch is zwakker dan geclaimd.** `save()` staat alleen op `GitProjectStore`,
  niet op de ABC, terwijl `project_manager.py:1376` hem aanroept; en `get_connector()` geeft een
  `GitConnector` aan vier modules. Een `DatabaseProjectStore` kan er dus niet "zonder
  caller-wijzigingen" onder. Deze escape hatch is precies wat C1 en C2 mogelijk maakte.

### C5. Piece A bewust niet gebouwd — akkoord

De checkpoint-diff (diff tegen de laatst *succesvol verwerkte* versie) is niet geïmplementeerd.
Reden is steekhoudend: de `runs`-tabel gaat over dev-run-sessies, niet over
verwerkings-checkpoints, dus er is een nieuwe tabel + Alembic-migratie nodig. Een half
aangesloten change-detection die stilletjes stopt met verwijderingen detecteren is erger dan
niets. De enablers (`read_at`, file-scoped `previous`, `MutationResult.ref`) zijn gebouwd en
getest. Losse follow-up.

---

## Deel D — Wat al WÉL geverifieerd is (voorkom dubbel werk)

Op de echte cluster met de branch gedraaid:

- **Eerste echte clone in de warme dir werkt.** Blobless clone
  (`--filter=blob:none`) tegen echte Forgejo, log: `ProjectStore warm working copy ready at
  /data/zad-projects-warm`.
- **Echte pushes werken, inclusief herhaalde edits.** Volledige levenscyclus, alles gepusht via
  de warme kopie:
  ```
  ccb6f3e Create project e2e97-ubn
  790f552 Add component 'apiworker' to project 'e2e97-ubn'
  7556463 Process project e2e97-ubn
  4d15d5a Delete deployment 'productie'
  3735189 Delete project 'e2e97-ubn'
  ```
  Dit is precies de "maak een project, voeg een component toe, werkt een **tweede** edit nog?"
  -check. Die slaagt.
- Unit: 55 tests groen op de gewijzigde bestanden; `ruff check`/`ruff format` schoon;
  `pyright` 0 errors op beide nieuwe modules.
- `previous()` is correct file-scoped (niet `HEAD~1`) — de expliciete correctheidseis uit het plan.
- Git-primitieven zijn degelijk: `%x1f`/`%x1e` scheidingstekens, dus een meerregelige
  commit-message kan niet als extra revisie worden misgeparsed.
- `ProjectManager`-eigendomsvlaggen kloppen (`:349,648,656,659`) — `project_manager.close()`
  sluit de warme connector niet. `startup.py` is correct meegenomen.

### Nog NIET geverifieerd
- C1 is nooit in-cluster afgevuurd (er trad geen ImagePullBackOff op). Trigger het met een
  component met een onbestaande image-tag en controleer daarna of een volgende project-edit
  nog werkt.
- Gelijktijdige lezers op de gedeelde werkboom (C2 in de praktijk).
- De ge-de-indenteerde `try/finally`-blokken in `startup.py` en `resource_tuning_service.py`.

---

## Deel E — Procesanalyse: wat ging er mis

Voor de retro, niet om iemand aan te wijzen.

1. **Contract omgedraaid, niet alle aanroepers meegenomen (C1).** `get_project_data_from_git`
   ging van "caller sluit" naar "caller mag niet sluiten". Eén van twee aanroepers is aangepast.
   *Les:* bij het omdraaien van een eigendoms-/levensduurcontract: grep alle aanroepers en
   verifieer ze stuk voor stuk; laat een CI-guard de invariant bewaken in plaats van een
   docstring.

2. **Gestopt bij "geen kubeconfig" terwijl de cluster bereikbaar was.** De dockersocket was
   gemount; `kind get kubeconfig` werkte. Daardoor is de hele PR op mocks beoordeeld terwijl
   juist deze bugklasse alleen in-cluster zichtbaar is. *Les:* controleer een blokkade langs
   twee wegen voordat je hem accepteert.

3. **Een onterechte dekkingsclaim stond even in de PR.** De docstring verwees naar
   `tests/test_git_store_primitives.py`, een bestand dat nog niet bestond. De auteur heeft dit
   zelf gevonden, ingetrokken en de tests alsnog geschreven (0612a64). Dat is precies de goede
   afhandeling — vermeld hier als voorbeeld, niet als verwijt.

4. **Twee reviewers maakten dezelfde vals-positieve bevinding.** Zowel de eerste review-scan als
   een subagent meldden `except ValueError, RuntimeError:` als Python-2-syntax en bijna als
   blocker. Onjuist: **PEP 758** (Python 3.14) staat exception-tuples zonder haakjes toe, het
   project gebruikt dit in 16 bestanden en de module importeert prima. *Les:* lokale tooling
   (hier Python 3.13) matcht de projectversie niet altijd — draai de code voordat je "parst
   niet" rapporteert.

5. **Test-rot in de e2e-suite (Deel A).** De wizard kreeg een stap erbij, de test is niet
   meegegroeid en faalt sindsdien op een manier die op een applicatiebug lijkt. Twee gevolgen:
   verspilde diagnosetijd, en — erger — een permanent rode suite waarin een echte regressie
   niet meer opvalt. *Les:* schrijf wizard-tests tegen een eindconditie ("tot de reviewstap"),
   niet tegen een vast aantal kliks.

6. **Falende tests stonden al langer rood.** Dat de 3 fouten identiek op `rc-2` optreden,
   betekent dat ze al vóór deze branch bestonden. Een rode baseline maakt elke volgende review
   duurder, omdat elke PR eerst moet bewijzen dat hij het níet veroorzaakte.

---

## Aanbevolen volgorde

1. **C1** — eenregelige fix + regressietest + CI-guard uitbreiden. Blokkeert RC-3.
2. **Deel A** — e2e-wizardtests herstellen (bij voorkeur robuust, niet op vast aantal kliks).
   Maakt de suite weer bruikbaar als signaal.
3. **Deel B** — eerst uitzoeken waaróm `automated` ontbreekt, dan pas herstellen.
4. **C2 + C3** — ontwerpgaten; vragen een besluit (slot afdwingen vs. `get_connector()`
   intrekken; niet-rebasende push).
5. **C4** — opruimwerk.
6. **C5 / Piece A** — losse follow-up met eigen scope.
