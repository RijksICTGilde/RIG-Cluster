# Ruwe metingen bij RC-117

De getallen in `docs/rc117-veel-acties-meting.md` komen hiervandaan, zodat een claim in
dat verslag terug te voeren is op een run en niet op een herinnering aan een run.

| Bestand | Wat het is |
|---|---|
| `sandbox-cf3b2346.json` | De uitvoer van `tests/e2e/test_sandbox_veel_acties.py` op commit `cf3b2346`: per actie de client-tijd, plus de samenvatting. |
| `opi-log-uittreksel-cf3b2346.txt` | De regels uit de OPI-log naast diezelfde run waar de opsplitsing per stap uit komt: `store-persist`, `store-push`, `store-lock`, de klonen, de ArgoCD-wachttijd en de totale taakduur. |

Het uittreksel is gefilterd, niet bewerkt: de tijdstempels zijn die van het cluster. Er
staan geen geheimen in - de git-URL's met wachtwoord staan op DEBUG-niveau in andere
regels en zijn niet meegenomen.

De lokale benchmark (`scripts/bench_project_writes.py`) is niet als bestand bewaard: hij
draait in een seconde of dertig en schrijft naar de terminal. De aanroepen staan
onderaan het verslag.
