# Sandboxrun 7 augustus 2026 — alles groen met de echte projectbestanden

Aanleiding: op 7 augustus 2026 zijn twaalf taken gemerged (RC-38 t/m RC-49, RC-51, RC-52,
RC-53). Elke PR is afzonderlijk geverifieerd, maar het geheel is niet tegen een draaiend
cluster gehouden met echte projectbestanden. Deze run doet dat.

Uitgevoerd op commit `f3882e2e` (branch `alles-groen-op-de-sandbox-met-de-echte-projectbest`).

## Samenvatting

Wordt ingevuld aan het eind van de run.

## 1. Cluster en context

| | |
|---|---|
| kubectl-context | `kind-rig-sandbox` (bevestigd, enige context) |
| sandbox-URL | https://zad.sandbox.rijksapp.dev |
| sleutel | sandbox, niet productie |

## 2. Testsets

Wordt ingevuld tijdens de run.

## 3. De 47 productiebestanden door de schemapoort

Bron: `robbert/rig-cluster-projects-github`, `projects/` (47 `*.yaml`), commit `30cfba1b3`.
Dat is dezelfde repo die `tests/test_upgrade_safety_replay.py` als `DEFAULT_PROJECTS_REPO`
noemt, dus de meting draait op de bestanden die de replay bedoelt.

### 3a. De blessed replay over de echte bestanden

```
RIG_PROJECTS_DIR=<checkout>/projects uv run pytest tests/test_upgrade_safety_replay.py -v
  -> 9 passed, 1 deselected
```

`test_real_project_files_migrate_and_validate` draait alle 47 door de exacte keten die
productie vóór een schrijfactie draait: `migrate_to_latest`, dan `validate_project_schema`,
dan `validate_project_structure` (inclusief de per-service typed-config gate). Alle 47 komen
er schoon door. De baseline-lijst met bekende defecten in dat bestand is nog steeds leeg.

### 3b. De poortmeting, vergeleken met 6 augustus

Los van de replay is de poort zelf gemeten, met dezelfde drie tellingen als
`features/project-schema-versions.md`:

| | 6 aug 2026 | deze run (7 aug) |
|---|---|---|
| bestanden met root-`domains:` van vóór v2.5 | 30 | **30** |
| bestanden met `config.keycloak`-restant van vóór v2.3 | 21 | **21** |
| afgekeurd door de poort (rauw, vóór migratie) | 22 | **0** |
| afgekeurd ná migratie | 0 | **0** |

Gedeclareerde schemaversies over de 47: versie `2` 5x, versie `2.2` 42x. 41 van de 47
bestanden migreren (`was_migrated`), en herschrijven zichzelf dus bij hun eerstvolgende
verwerking — verwacht gedrag, geen bevinding.

**De 22 zijn 0 geworden, en dat is de bedoelde winst, geen regressie.** Op 6 augustus
valideerde `git_monitor` rauwe inhoud tegen het *nieuwste* schema; 22 bestanden vielen
daardoor stil buiten de verwerking. Sinds RC-32 valideert de poort tegen de versie die het
bestand zelf declareert (`validate_declared_project_schema`), en halen alle 47 het.

Ter controle is ook de *oude* poortvorm nagebootst — rauwe inhoud tegen het nieuwste schema
(2.6). Dat geeft nu **34** afkeuringen, meer dan de 22 van 6 augustus. Dat getal is geen
regressie maar de verwachte uitkomst van precies de verandering die RC-32 mogelijk maakte:
het nieuwste schema heeft sindsdien de oude vormen `domains:` (v2.5) en `config.keycloak`
(v2.3) laten vallen, dus meer oude bestanden botsen ermee. Dat pad wordt niet meer gelopen;
het is hier alleen gemeten om te laten zien waar het verschil vandaan komt.

Geen enkel bestand dat op 6 augustus verwerkt werd, wordt vandaag geweigerd. De schemapoort
van RC-44 verandert dit oordeel niet: die weigert vroeg in de *wizard*, en de 47 bestanden
komen niet via de wizard binnen.

## 4. Bevindingen

Wordt ingevuld tijdens de run.
