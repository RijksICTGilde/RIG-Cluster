# platform.json

Instructie voor wie (mens of LLM) `operations-manager/python/opi/services/zad_map/platform.json` tegenkomt: in een rode test, in een diff of bij een merge.

## Wat het is

De data achter de platformkaart van ZAD (`features/zad-kaart.md`): per systeem (Kubernetes, Keycloak, MinIO, Prometheus, ...) wat het is, in welk cluster het draait, welke OPI-connector ermee praat, welke catalogus-services erop leunen en welke feature-docs erover gaan. `opi/services/zad_map/platform.py` leest het en bouwt er de graaf van; de route staat in `opi/web/router.py` (`platform_graph()`).

Het is **gegenereerd** en staat toch in git. De reden: de bronnen (`infrastructure/` en `features/`) worden niet in het image gekopieerd (zie de Dockerfile), dus de draaiende OPI kan het niet zelf opbouwen.

## Regels

- **Nooit met de hand bewerken.** Altijd regenereren:
  ```bash
  cd operations-manager/python
  uv run python -m opi.tools.zad_map_platform            # schrijven
  uv run python -m opi.tools.zad_map_platform --check    # exit 1 als het achterloopt
  ```
- **Regenereren na de rebase, niet ervoor.** De uitvoer hangt af van de hele repo, dus een `platform.json` die op je eigen branch klopt, kan na een rebase op main alweer achterlopen. Bij een conflict in dit bestand: neem een van beide kanten en regenereer.
- **Commit het mee met de wijziging die het veranderde**, of als losse commit `fix(kaart): platform.json opnieuw gegenereerd na <wat>`.
- Een rode `tests/test_zad_map_platform.py::test_checked_in_file_matches_the_repo` is bijna altijd alleen dit: regenereren en de diff bekijken. Klopt de diff niet met wat er veranderd is, dan zit de fout in een handtabel (hieronder), niet in het bestand.

## Waar de inhoud vandaan komt

De generator is `opi/tools/zad_map_platform.py`. Vier bronnen lezen het automatisch:

| Bron | Wat eruit komt | Wat het bestand dus verandert |
|---|---|---|
| `infrastructure/bootstrap/clusters/<cluster>/kustomization.yaml` voor `odcn`, `odcn-staging`, `sandboxed-local` | per systeem `aan`, `uit` (regel met `#` ervoor), `elders`, `afwezig`, `cluster` of `extern` | een component aan- of uitzetten of toevoegen in een cluster |
| `opi/**/*.py`, regex `from opi\.connectors\.<naam>` | per connector het aantal imports (`uses`), buiten `opi/connectors/` zelf | **elke** import van een connector die erbij komt of verdwijnt, ook in een wijziging die niets met het platform te maken heeft |
| `opi/services/catalog/<dir>/help.md` | de titel (eerste `# `-regel) van een catalogus-service | de kop van een `help.md` wijzigen |
| `features/*.md` | per systeem de docs waarvan de bestandsnaam met een voorvoegsel uit `DOC_PREFIX` begint, met hun titel, maximaal 8 | een feature-doc toevoegen, hernoemen of zijn kop wijzigen |

Het `uses`-getal is de grootste bron van onverwacht rood: een refactor die een import van `grafana_prometheus` of `kubectl` toevoegt of weghaalt, laat het bestand al achterlopen. De kaart toont het alleen als "(Nx geimporteerd)" in het infopaneel.

## De handtabellen

Bovenin de generator staan tabellen die niet uit de code volgen. Ze worden niet gecontroleerd: wat er niet in staat, valt **stil** weg van de kaart.

| Tabel | Bijwerken als je ... |
|---|---|
| `SYSTEMS` (titel, groep, omschrijving) en `LAYER` | een systeem toevoegt. Beide nodig: ontbreekt het in `LAYER`, dan crasht de generator met een `KeyError`. De id is de mapnaam onder `infrastructure/bootstrap/infrastructure/` |
| `CONNECTOR_SYSTEM` | een connector toevoegt in `opi/connectors/` die met een systeem praat. Interne connectors (`http`, `opi`, `subdomain`, `git_repo`) staan er bewust niet in |
| `SERVICE_SYSTEM` | een catalogus-service toevoegt |
| `CATALOG_DIR` | een service toevoegt waarvan de map in `opi/services/catalog/` niet gelijk is aan de naam met `-` vervangen door `_` |
| `DOC_PREFIX` | een feature-doc schrijft over een systeem met een bestandsnaam die bij geen bestaand voorvoegsel past |
| `CLUSTERS` | een clustermap toevoegt die op de kaart moet |

Na het bijwerken van een tabel: regenereren, en in de diff controleren dat precies het verwachte systeem, de connector, de service of de doc erbij kwam.

## Open

Dat het bestand na veel merges achterloopt, staat als issue #341 op het ZAD-bord.
