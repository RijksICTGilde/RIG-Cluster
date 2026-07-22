# Project-store timing-logging

De `GitProjectStore` logt hoe lang een schrijfoperatie op de lock wacht, hoe lang hij de lock
vasthoudt, en hoe lang de git-push duurt. Bedoeld om te kunnen zien waar de doorvoer op vastloopt.

## Waarom

Alle projectfile-writes serialiseren op één process-wide `asyncio.Lock`. Eén trage schrijf vertraagt
dus de volgende. Bij vijf gelijktijdige component-adds zag je commits met 6-7 seconden ertussen
binnenkomen, en zonder meting is niet te zeggen of dat komt doordat writes op de lock in de rij
staan (wachttijd) of doordat één schrijf zelf traag is (houdtijd, gedomineerd door de push).

## Wat er gelogd wordt

In `opi/services/project_store.py`, per schrijfoperatie (`create`, `save`, `mutate`, `delete`) en
per `reconcile`:

```
store-lock <operatie> '<project>': waited 3.10s for the lock
store-lock <operatie> '<project>': held 2.80s (waited 3.10s)
store-push ['projects/foo.yaml']: push took 2.51s
store-persist projects/foo.yaml: commit+push took 2.58s
```

- **waited** - hoe lang deze schrijver op de lock stond. Hoog = de store staat in de rij (contentie).
- **held** - hoe lang de lock werd vastgehouden. Hoog = deze ene schrijf is traag.
- **push** - alleen de `git push`. Dit is de bekende ~2,5s (server-side Forgejo-verwerking; de
  netwerk-round-trip is 0,07s).
- **persist** - commit-opbouw plus push samen. Het verschil met push is de commit-opbouw.

## Niveaus

Standaard op INFO, zodat het meedraait zonder debug-logging aan te zetten. Boven een drempel gaat
het naar WARNING, zodat een operator contentie ziet zonder te filteren:

- wachttijd >= `_LOCK_WAIT_WARN_SECONDS` (2,0s)
- houdtijd/persist >= `_PERSIST_WARN_SECONDS` (3,0s)

Stel de drempels bij op wat de logs in de praktijk laten zien.

## Interpretatie

- Veel **waited**, weinig **held**: de doorvoer wordt geremd doordat writes serialiseren, niet doordat
  een schrijf traag is. Dit is de structurele bottleneck van één repo/branch en één lock; de
  oplossing zit in het ontwerp (repo per project, of de push van het request-pad af), niet in de
  store zelf.
- Weinig **waited**, veel **push**: de push zelf is traag. Dat staat los van deze store en wijst naar
  Forgejo's `receive-pack`-verwerking.

## Test

`tests/test_project_store.py::test_mutation_logs_lock_and_persist_timing` controleert dat de drie
regels met hun operatie-label worden uitgezonden (niet de waarden, die zijn omgevingsafhankelijk).
