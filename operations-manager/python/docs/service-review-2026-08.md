# Service review — augustus 2026

Controle achteraf van alle services in `opi/services/catalog/` tegen
`instructions/service-review-checklist.md`. De service-opzet is op 1 augustus 2026
afgemaakt; die checklist is pas daarná geschreven. Dit is de controle achteraf: per
service een checklisttabel (secties 1-12), de bevindingen eronder, en — waar het
aantoonbaar en veilig kon — de reparatie zelf (met een test die eerst faalde op de oude code).

## Werkwijze

Per service is de waarheid uit de registry gelezen, niet uit bestandsnamen
(`SERVICES[ServiceType.X].config_model`, `.config_schema_version`,
`.config_model_for(layer)`), precies omdat `persistent-storage` en `temp-storage` hun model
delen via `catalog/shared/storage.py` en `minio`/`postgresql-database` `CloneState` uit
`catalog/shared/revisions.py` mixen. Daarna is de checklist per sectie afgelopen en is elke
lezer/schrijver van de config (managers, forms, generation) nagelopen.

Elke cel in de tabellen is `PASS`, `FAIL` of `N.v.t.` met reden. Elke `FAIL` heeft eronder een
bevinding: het bestand en de regel, wat er mis is, de gevolgen, en of het gerepareerd is of
waarom niet. Reparaties vallen strikt binnen sectie 3 van het plan ("veilig te repareren");
al het andere is vastgelegd als bevinding met een aanbeveling.

## Reikwijdte van de verificatie

Checklistsectie 9 ("verify against real project files") vraagt om de audit over de 47
productie-projectbestanden in de externe repo
`~/IdeaProjects/rig-cluster-test-git-repositories/rig-cluster-projects-github/projects`. **Die
repo is in deze omgeving niet aanwezig.** De audit is daarom gedraaid over de wél beschikbare
echte vormen: `projects/simple-example.yaml` (repo-root) en de fixtures onder
`operations-manager/python/tests/fixtures/` en `tests/golden/`. Waar sectie 9 op een service
van toepassing is, staat dit als beperking genoteerd; de conclusie "elke config-blok wordt door
zijn model geclaimd" is dus getoetst tegen de beschikbare data, niet tegen de volledige
productieset.

## Testbaseline

Vóór enige wijziging: `uv run pytest tests/ -q` gaf **4850 passed, 6 skipped, 32 errors**. De 32
errors zijn uitsluitend de `@pytest.mark.kind`/`integration`-tests in
`tests/integration/test_kubectl_write_ops.py`, die in hun fixture een echte Kind-cluster
proberen op te zetten (`tests/integration/conftest.py:210`); dat kan zonder Docker/Kind in deze
omgeving niet. Ze zijn omgevingsgebonden en niet door deze branch veroorzaakt. Alle
reparaties hieronder houden de pass-telling gelijk of hoger en voegen geen nieuwe failures of
errors toe.

---

## Samenvatting

_(wordt ingevuld nadat alle services zijn nagelopen)_

---
