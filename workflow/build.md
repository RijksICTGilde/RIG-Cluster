- UI components must use the Lord of the Components tags (`<c-*>`, NLDD-thema) as much as possible; if a component seems to be missing, add it to the list in request_for_components.md with a detailed request for it so it can be built later. See `features/lotc-bouwlijn.md`.
- **Never put a `{# ... #}` comment INSIDE a component tag.** The LOTC preprocessor parses the tag body as attributes, so the words in your comment become attribute names and rendering dies with `Duplicate attribute 'een'` — a message that points at a word, not at the comment. It has cost two reworks. Put the explanation above the tag:

  ```jinja
  {# WRONG: dies on 'Duplicate attribute'                 RIGHT: #}
  <c-button                                               {# why this icon is filtered #}
      {# why this icon is filtered #}                     <c-button
      :icon="x | nldd_icon" />                                :icon="x | nldd_icon" />
  ```

- **Schrijf de gebruiker aan met "je", nooit met "u".** Dat geldt voor alles wat op het scherm komt: bevestigingen, meldingen, labels, hulpteksten en foutmeldingen. Het portaal is een gereedschap voor collega's, geen brief van de overheid aan een burger. Kom je "u" of "uw" tegen in bestaande tekst, zet het om.

- Python imports must always be at the top of the file, never inline or local. Use `ruff check --select I --fix` to sort and organize imports, then `ruff format .` to format.
- **Build fast, test locally.** Only run the tests for what you changed (`uv run pytest tests/<file> -x -q --tb=short`), never the full suite. The unit suite is ~7900 tests and the browser suite ~350; running either in full costs minutes per attempt and is not your job — the full run happens once before the merge to main. Pick the files that cover your change; if you cannot name them, that is the signal your change lacks a test, not that you should run everything.
- **Look at the screen for anything visual.** A green browser test does not prove a table renders as a table. Take a screenshot of what you built and judge it. This has been the cause of six reworks.
- **Never sleep for a fixed period when you can wait for the condition.** A task whose tests take three minutes has taken two hours, and the difference was waiting, not working. `sleep 300` costs five minutes even when the thing was ready in ten seconds, and it costs another five when it was not. Wait on the thing itself:
  - `until <check>; do sleep 5; done` returns the moment the condition holds. Poll a *local* command every 5-15s — it costs nothing. A remote API with rate limits: 30s.
  - Use the waits that already exist rather than building your own: `kubectl wait --for=condition=Ready pod/... --timeout=120s`, `kubectl rollout status deployment/... --timeout=120s`, `wait_for_task()` in the test helpers. They return on success instead of on the clock.
  - **A timeout is a safety net, not a waiting mechanism.** `--timeout=600` means "something is wrong if it takes this long", never "come back in ten minutes". If your wait only ever ends at the timeout, you are not waiting, you are guessing.
  - Waiting on something that reports progress? Read the progress, do not re-check on a timer. A rollout, a task and a pod all say when they are done.
- **Do not poll a log file for a command you are waiting on anyway.** This is the pattern that cost hours:

  ```
  # WRONG: a test run in the background, then grepping its own output for the summary line
  for i in $(seq 1 19); do
    grep -qE "= .*(passed|failed).* in .*=" e2e.log && break
    sleep 30
  done          # timeout 10m
  ```

  Three things go wrong at once. There is no parallelism, because you block on it regardless. The poll interval is added latency on top of the real runtime. And worst: if the command dies *without* writing that line (a crash, an OOM, a killed worker), the loop never matches, runs every iteration, and ends on the timeout — so a failure that took ten seconds presents itself as ten minutes of waiting.

  - Waiting for it anyway? **Run it in the foreground** with a sane timeout. You get the output and the exit code the moment it ends, and a crash is immediately a crash.
  - Genuinely doing something else meanwhile? Background it and wait on the **process**, not on its output — the exit code tells you what happened, an absent log line does not.
  - A log file is for reading afterwards, or for following a long-running service. It is not a completion signal.

- **Een lange run moet zichtbaar zijn TERWIJL hij loopt.** Een sandboxsuite duurt bijna een
  uur en pytest zegt tot het eind niets bruikbaars, dus wie meekijkt ziet niets en kan niet
  beoordelen of het loopt, hoe snel, of waar het strandde. Zet daarom `PYTEST_VOORTGANG`:

  ```bash
  PYTEST_VOORTGANG=/tmp/voortgang.txt uv run pytest ... &
  tail -f /tmp/voortgang.txt
  ```

  Dat schrijft per afgeronde test een regel met tijdstip, `n/totaal`, duur, het aantal rode
  tot nu toe, de uitslag en de nodeid - uit pytest's eigen `report`-object
  (`tests/conftest.py`), dus niet uit de uitvoer gegrepen. Zonder de variabele verandert er
  niets aan een gewone run.

  Draai je zo'n run voor iemand anders, deel die regels dan ook echt: een run op de
  achtergrond met de uitvoer in een bestand is voor de ander hetzelfde als stilte.

- **En wacht niet op je eigen achtergrondrun.** Dit is de fout die in RC-128 uren kostte en
  waar de opdrachtgever op corrigeerde. Start je een suite op de achtergrond, dan **meldt de
  harnas zelf** dat hij klaar is, met exitcode. Er daarna in blijven hangen met

  ```bash
  until [ -s $S/tasks/<id>.output ]; do sleep 45; done   # FOUT
  ```

  is dubbelop en puur verlies: elke ronde kost een tool-call, de pauze is latentie boven op
  de echte looptijd, en de Bash-timeout is **maximaal 10 minuten** - bij een suite van een uur
  tuig je die lus dus zes keer op en levert hij zes keer niets op.

  De werkwijze die wel loopt:

  1. **Achtergrond + de notificatie als signaal.** Niet erop wachten; er ander werk bij doen.
  2. **Eén `Monitor` op het voortgangsbestand voor vroeg rood**, met een filter dat ook falen
     dekt: `tail -f voortgang.txt | grep -E --line-buffered ' (FAILED|ERROR) '`. Zo weet je
     binnen seconden van een rode test in plaats van pas na een uur.
  3. **Kies werk dat niet botst.** Metingen die alleen LEZEN (schermen bekijken, `kubectl`,
     greps op de bron) kunnen naast een suite. Alleen wat dezelfde staat MUTEERT - hetzelfde
     cluster, dezelfde `zad-projects` - moet wachten.
  4. **Moet je het antwoord hebben voor je verder kunt?** Dan in de voorgrond met een timeout.
     Kan het langer dan tien minuten duren, dan kan dat niet, en is achtergrond de enige
     juiste keuze.

- **Meet eerst of de run kán slagen.** Een sandboxsuite die op capaciteit vastloopt kost uren
  en levert een omgevingsartefact op, geen oordeel. De node is **één** node met `4 cpu` en
  **max 110 pods**; `kubectl describe node` geeft de cpu-requests. Doet een test veel langer
  dan in een vorige gang (en `PYTEST_VOORTGANG` maakt dat zichtbaar), kijk dan eerst naar
  `FailedScheduling` in de events voordat je de code verdenkt.

- **Na een bulkverwijdering: laat het cluster eerst tot rust komen.** Een suite starten terwijl
  namespaces en CNPG-clusters nog aan het opruimen zijn laat de eerste test die opruiming
  meten. In RC-128 gaf dat vijf ERRORs die losstaand meteen groen waren.

- **Ask the thing that knows, not the clock.** Sleeping until something is "probably done" is guessing twice: about the time, and about the outcome. Every state you might sleep on has an owner that will tell you:
  - a task → the task endpoint (`wait_for_task()` in the test helpers wraps it, and returns the *outcome*, not just "finished");
  - a deployment's health and sync → ArgoCD, via `opi/services/argocd_overview.py` for a whole project in one query;
  - a pod → `kubectl wait` / `kubectl rollout status`;
  - a project's state → the API, which is also what the zad-cli talks to and will keep talking to.

  This is not only about speed. E2E wizard tests once checked the *file state* instead of asking the task how it ended, and stayed green through a broken create — a sleep long enough to "be safe" hides a failure exactly as well as it hides a delay.
