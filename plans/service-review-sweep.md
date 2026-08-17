# Alle services nalopen tegen de reviewchecklist

**Opdracht:** loop elke service in `opi/services/catalog/` na tegen `instructions/service-review-checklist.md`, repareer wat aantoonbaar en veilig te repareren is, en leg de rest vast als bevinding met een aanbeveling.

**Waarom nu:** op 1 augustus is de service-opzet afgemaakt (dertien van de vijftien services hebben een configmodel en een drift-gelockt fragment, `validate_service_configs` loopt over alle vier de configlagen, en er staan geen servicenamen meer als contract in het globale schema). Dat werk is per service gedaan, deels onder tijdsdruk, en de checklist is pas dáárna geschreven. Deze sweep is de controle achteraf.

Alle paden zijn relatief aan `operations-manager/python/` tenzij ze met `instructions/`, `features/`, `plans/` of `opi/schemas/` beginnen.

---

## 1. Wat je oplevert

Twee dingen, in één PR:

1. **`docs/service-review-2026-08.md`**: per service een tabel met per checklistsectie pass, fail of niet van toepassing, en onder de tabel de bevindingen uitgeschreven. Een bevinding noemt het bestand en de regel, wat er mis is, wat de gevolgen zijn, en of je het gerepareerd hebt of waarom niet.
2. **De reparaties zelf**, met tests, voor alles wat onder "veilig te repareren" valt (sectie 3).

Wat je niet oplevert: een herstructurering. Dit is een controle, geen tweede migratieronde.

## 2. De services

Vijftien in `opi/services/catalog/`, plus `shared/` dat geen service is maar wel meegenomen wordt omdat twee services erop leunen:

`attachments`, `authorization_wall`, `health_check`, `keycloak`, `metrics_scraper`, `minio`, `namespace_postgres`, `namespace_redis`, `persistent_storage`, `platform`, `postgresql_database`, `publish_on_web`, `redis`, `sleep_mode`, `temp_storage`.

`namespace_redis` en `platform` dragen bewust geen config. Voor die twee is de vraag niet "is het model goed" maar "klopt het dat ze er geen hebben", en dat toets je tegen echte projectbestanden en tegen wat hun manager leest.

## 3. Wat je wel en niet zelf repareert

**Wel, direct, met een test die eerst faalt op de oude code:**

- een ontbrekende validator op een editable met een begrensde waardenreeks;
- een ontbrekende `remove_when_none` op een optioneel veld, met de uitzondering uit de checklist (nooit op een boolean met default `True`);
- een ontbrekende converter waardoor een leeg veld als `''` of `[]` wordt weggeschreven;
- een logregel die een heel object formatteert, teruggebracht tot identificerende waarden;
- een ontbrekende logregel op een toestandswijziging, of een regel die op elke run vuurt terwijl er niets veranderde;
- een lezer die identiteit bepaalt met een sleutel-lookup of een JSONPath in plaats van `service_entry_name`;
- een hardgecodeerd yaml-pad waar `config_path(...)` hoort;
- een verouderde docstring of comment die feitelijk onwaar is geworden.

**Niet zelf, wel vastleggen als bevinding met aanbeveling:**

- alles wat gedrag in een publieke stroom verandert;
- alles wat een projectbestand op schijf raakt;
- een configlaag toevoegen of verwijderen;
- een schemaversie ophogen (zie sectie 5, dat is nieuw terrein);
- iets dat een productbeslissing vraagt, zoals of een veld verplicht hoort te zijn;
- het globale schema aanpassen.

Bij twijfel: vastleggen, niet repareren. Een PR die halverwege van controle naar verbouwing kantelt is niet te reviewen.

## 4. Werkwijze per service

1. Lees `instructions/service-review-checklist.md` volledig voordat je begint. Sectie 0 is geen inleiding maar een instructie.
2. Stel via de registry vast wat de service declareert, niet via bestandsnamen. Twee inventarisaties gingen daar eerder mis omdat `persistent-storage` en `temp-storage` hun model delen via `catalog/shared/storage.py`.
3. Draai de audit uit checklistsectie 9 over alle productiebestanden in `~/IdeaProjects/rig-cluster-test-git-repositories/rig-cluster-projects-github/projects` (47 stuks): inlezen, `migrate_to_latest` in memory, dan `validate_project_schema` en `validate_service_configs`. Dat is de enige manier om te weten welke lagen een service werkelijk draagt. **Alleen lezen. Die repo is productiedata en er wordt niets in gewijzigd of gecommit.**
4. Loop de checklist af en noteer per sectie de uitkomst.
5. Repareer wat onder sectie 3 valt, met een test die je eerst hebt zien falen op de oude code.
6. Draai de guardrails (sectie 6 hieronder) voordat je naar de volgende service gaat.

**Verify per service:** de checklisttabel is volledig ingevuld (geen lege cellen), elke fail heeft een bevinding of een reparatie, en de guardrails zijn groen.

## 5. Vier dingen die je bij voorbaat moet weten

Deze zijn gemeten, niet vermoed, en ze schelen je een verkeerde conclusie.

**`config_schema_version` en `config_model` reizen samen.** Sinds 1 augustus is de default `None` en bewaakt `tests/test_service_config_schema.py` de koppeling. Een service met een versie maar zonder model is dus geen bevinding meer maar een testfout.

**Geen enkele service overschrijft `migrate_config`.** Alle modellen staan op 1.0, dus de per-service migratieweg is er wel maar is nooit gebruikt. Constateer dat, maar hoog geen versie op om het te proberen; dat is een aparte taak.

**Drie lagen worden door `validate_service_configs` gelopen sinds 1 augustus, plus de deployment-component-laag.** Een service die daar config draagt en geen model heeft is een echt gat. Meet het, ga niet af op de code van vóór die datum.

**Het globale schema is opgeruimd.** `$defs/publish-on-web-config` en `$defs/attachment-use-entry` zijn verwijderd omdat hun kennis nu in de servicemodellen zit. Wat er nog wel staat en bewust blijft: de root-`domains`-defs en de v0-boolean `publish-on-web`, want `git_monitor.py:137` valideert een bestand rechtstreeks uit git vóór enige migratie. Stel niet voor die weg te halen; dat hangt aan het blokkerende punt over per-versie schemavalidatie in `TODO.md`.

## 6. Guardrails

Na elke service, en nog een keer voordat je de PR opent:

```bash
cd operations-manager/python
uv run pytest tests/test_service_providers.py tests/test_service_config_schema.py \
              tests/test_golden_manifests.py tests/test_flow_registry_snapshot.py -q
uv run ruff check . --fix && uv run ruff format . && uv run pyright
uv run pytest tests/ -q
```

De volledige suite eindigt op nul failures en nul errors. Een error door een ontbrekende afhankelijkheid of een vervuilende test telt gewoon mee als rood; die zijn op 1 augustus allemaal opgelost, dus als er een terug is heb jij hem veroorzaakt.

## 7. Volgorde

De vijftien services zijn onderling onafhankelijk, dus de volgorde is vrij. Aanbevolen is te beginnen met de vier die op 1 augustus als laatste een model kregen en dus het minst zijn uitgehard: `redis`, `publish_on_web`, `attachments`, `minio`. Daarna de twee die op een gedeeld model leunen (`persistent_storage`, `temp_storage`), want daar zit de enige `config_model_for`-override. Dan de rest.

## 8. Wat succes is

- Elke service heeft een ingevulde checklisttabel.
- Elke fail is gerepareerd of vastgelegd met een aanbeveling en een reden.
- De volledige testsuite is groen.
- `docs/service-review-2026-08.md` is te lezen door iemand die er niet bij was.
- De PR bevat geen herstructurering en geen wijziging aan een projectbestand.
