# Review: opvolging bevindingen ronde 3 (PR #6)

Je bent **reviewer**. Maak geen codewijzigingen tenzij de gebruiker daar expliciet
om vraagt. Als dat toch gebeurt: teken je eigen werk niet af, maar draag over aan
een verse sessie. Dat is in deze PR al drie keer aan de orde geweest.

## Wat je beoordeelt

```
git log 302b08d..HEAD --oneline     # 7 commits
git diff 302b08d..HEAD --stat
```

| commits | wie | beoordeeld? |
|---|---|---|
| `5c72cef..487fb4e` | sessie 1 | ja, iteratie 5 + 6 |
| `be90af4..302b08d` | sessie 2 | ja, iteratie 7 (RC-3 approved) |
| `e1015a3..63c0847` | sessie 3 | **nee — jouw taak** |

RC-3 is en blijft goedgekeurd. Dat ging over het werk van sessie 2. Deze zeven
commits zijn daarna gemaakt door de reviewer van ronde 3, die op verzoek van de
gebruiker implementer werd, en zijn door niemand bekeken.

## Uitgangssituatie

Sessie 3 begon als onafhankelijke review, vond vijf bevindingen, keurde RC-3 goed,
en kreeg daarna de opdracht: "los alles op, inclusief de fouten die al bestonden,
en test tegen het sandbox cluster."

| Gate | Bij aanvang | Nu |
|---|---|---|
| `pytest tests/ --ignore=tests/integration` | 4093 passed, 7 failed | **4123 passed, 0 failed** |
| `pytest tests/integration/…` (3 bestanden) | 90 passed | 90 passed |
| `ruff check .` | 7 errors | schoon |
| `ruff format --check` | 2 te formatteren | schoon |
| `pyright` | 0 errors | 0 errors |

## Zelf verifiëren

```bash
cd operations-manager/python
uv run pytest tests/ -q --ignore=tests/integration   # verwacht: 4123 passed, 0 failed
uv run pytest tests/integration/test_api_endpoints.py tests/integration/test_edge_cases.py \
              tests/integration/test_project_api.py -q   # verwacht: 90 passed
uv run ruff check . && uv run ruff format --check . && uv run pyright
```

**Draai `tests/integration/` mee.** De unit-baseline sluit die map uit en dat is
precies waar de regressie van ronde 2 zichtbaar werd.

Let op: `GIT_AUTHOR_NAME` en `GIT_AUTHOR_EMAIL` staan in deze container op **leeg**,
wat git-commits blokkeert (`fatal: empty ident name`). Gebruik
`env -u GIT_AUTHOR_NAME -u GIT_AUTHOR_EMAIL git …`. De tests hebben hier geen last
meer van sinds `f336ed6`; committen wel.

## Waar ik zou beginnen met twijfelen

Behandel dit als hypotheses, niet als waarheid.

1. **`_conflicting_added_keys`** (`project_store.py`, commit `e1015a3`). Nieuw, en het
   zit in het hart van de concurrency-oplossing. Het meldt een conflict wanneer beide
   schrijvers dezelfde eerder afwezige sleutel aanmaken met een *afwijkende* waarde.
   Klopt het faalgedrag in gevallen die sessie 3 niet bedacht heeft? Genest? Lijsten?
   Sleutels die naar `None` gaan? Is "zelfde waarde = overeenstemming" altijd veilig?

2. **De `record_base=False`-conventie** (commit `48f6a41`). Tien projectie-helpers
   lezen nu zonder de compare-and-swap-basis te verzetten. De regel staat in
   `features/project-store.md` en in de docstring van `get_contents`, **maar er is geen
   test die hem afdwingt**. Een nieuwe helper die `get_contents()` zonder de parameter
   aanroept herintroduceert de bug stilzwijgend. Een guard-test in de trant van
   `tests/test_single_path_enforcement.py` zou dat kunnen afvangen — beoordeel of dat
   hier moet.

3. **Het ruamel-typeverhaal** (commit `63c0847`). De merge werkt op `CommentedMap`/
   `CommentedSeq`, en een gewone `dict` ernaast geeft een type-wijziging op root
   waardoor élke gelijktijdige save botst. Sessie 3 stelt dat productie die types
   nergens mengt. **Verifieer dat zelf** — de hele compare-and-swap hangt eraan.
   Aandachtspunten: `read_path()` cache-tak versus git-tak, `_refresh_cache`,
   `load_project_from_data`, en wat de wizard/forms-laag teruggeeft.

4. **De verruimde componentnaam-regel** (commit `0a99317`). Sessie 3 meldde in de
   review eerst "een echte productiebug: de API accepteert een ongeldige naam met
   202". **Dat was fout.** Koppeltekens en 63 tekens zijn sinds `f620721`/`785a2b7`
   (11 juli) bewust geldig, in lijn met `project_v2.json`, met 72 tests die dat
   asserten. De vijf falende tests dateerden van 13 maart. Ze zijn bijgewerkt naar de
   echte grens. Controleer dat dit klopt en dat er geen test is verzwakt in plaats van
   gecorrigeerd — dit is het commit waar de kans op een foute "fix" het grootst was.

5. **Testkwaliteit.** Sessie 3 heeft elke toegevoegde test gefalsifieerd (bug terug →
   test faalt → bug weg → test slaagt) en dat per test gerapporteerd. Steekproef dat:
   pak twee tests uit `fef93e8` en `e1015a3` en probeer ze te laten slagen met een
   kapotte productiecode.

## Sandboxverificatie die al gedaan is

Image `rc-5` (thin overlay op `rc-2`) uitgerold op de Kind-sandbox `rig-sandbox`, en
het schrijfpad uitgeoefend tegen de **echte** Forgejo-remote, niet tegen een fake
connector:

```
OK 1: zelfde nieuwe veld -> ConflictError, hun waarde intact
OK 2: ongerelateerde velden gemerged, beide overleefd
OK 3: twee toegevoegde componenten beide overleefd -> ['alpha','bravo','charlie']
```

`ProjectStore bootstrap` draaide schoon in de pod. Daarna teruggerold naar `rc-2`,
testproject `sbxchk` opgeruimd (geverifieerd: `projects/` bestaat niet meer in
Forgejo, dus leeg).

Het verificatiescript staat in de scratchpad van sessie 3 en is **niet gecommit**.
Wil je het herhalen, dan moet je het opnieuw schrijven — overweeg of dit script in de
repo hoort, bijvoorbeeld als sandbox-e2e-test. Sessie 3 heeft dat bewust niet gedaan
(scope), maar het is de enige verificatie die de merge tegen echte git uitoefent.

Herhalen:
```bash
kind get kubeconfig --name rig-sandbox > /tmp/kubeconfig && export KUBECONFIG=/tmp/kubeconfig
# Dockerfile.thin: FROM operations-manager:rc-2
#                  + COPY operations-manager/python/opi /app/opi
#                  + COPY operations-manager/python/manifests /app/manifests
docker build -f Dockerfile.thin -t operations-manager:rc-N .   # vanuit /workspace!
kind load docker-image operations-manager:rc-N --name rig-sandbox
kubectl -n rig-system set image deployment/operations-manager operations-manager=operations-manager:rc-N
```
Selecteer de pod **op image**, niet op label. Rol na afloop terug naar `rc-2`.
`/version` blijft leeg bij een thin image — dat is verwacht, dus verifieer op image.

## Buiten scope gelaten (bewust) — kandidaten voor eigen taken

- **`get_decrypted_view()` heeft nul productie-aanroepers.** Dode code; niet verwijderd
  omdat CLAUDE.md zegt dat te melden en niet te doen.
- **`generate_hostname` in `opi/utils/naming.py` heeft geen gecombineerde
  lengtebegrenzing.** Component + deployment + project + domein kan de DNS-limiet van
  63 tekens overschrijden; er is geen guard en geen truncatie. Gevonden bij het
  uitzoeken van de naamregel. Dit is een echte latente bug, los van deze PR.
- `mutate()` dekt 1 van de 34 schrijfpaden (grotendeels achterhaald door de
  structurele merge).
- Forgejo-webhook voor proactieve detectie van externe edits.
- `store.create()` / `store.delete()` zonder eigen test.
- Piece A (checkpoint-diff), doorvoer 2,6s per push, wizard-e2e testrot,
  ArgoCD `syncPolicy.automated`.

## Afronden

1. `pr-comment "## Review opvolging ronde 3\n..."` met je bevindingen
2. `orch review-summary <TASK-ID> "..." -v approve|rework --base-commit 302b08d --head-commit $(git rev-parse HEAD)`
3. Bij akkoord `orch approve <TASK-ID> -c "reden"`, anders `orch feedback <TASK-ID> "..."`
4. `stop-session`

Valkuilen: `pr-comment --help` plaatst een comment in plaats van help te tonen.
Het project draait **Python 3.14** — `except A, B:` zonder haakjes is daar geldig
(PEP 758); draai code via `uv run` voordat je "parst niet" meldt.
