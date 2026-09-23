# scripts/ (repowortel)

Gereedschap dat op de REPOSITORY werkt en niet op een draaiende OPI. Dat is het verschil met
`operations-manager/python/scripts/`: die tools praten met een cluster, met Keycloak of met een
projectbestand in de zad-projects-repo, en draaien vanuit `operations-manager/python`. Deze staan
hier omdat ze paden in de repowortel aanraken (`bootstrap/`, `infrastructure/`, `security/`,
`projects/`, de historie) en dus vanaf de wortel horen te worden aangeroepen.

## De sleutelrotatie

Zie `features/sops-sleutel-vervangen.md` voor het hele verhaal, inclusief de metingen.

| ingang | doet |
|---|---|
| `rotate-sops-key.py` | de SOPS-bestanden, de losse versleutelde waarden en `projects/` in deze repo, plus met `--argo-applications` de ArgoCD repository-secrets in een clone van zad-argo-user-applications; `--verify` en `--assert-old-key-dead` |
| `rotate-project-keys.py` | de projectbestanden in een clone van de projects-repo, commit per project |
| `replace-git-pat.py` | dezelfde ronde, met de GitHub-PAT er ook vervangen |
| `set-sops-key-secret.py` | het k8s-secret `sops-age-key` wisselen en de operations-manager herstarten |

De logica zit in modules ernaast, want een streepje in een bestandsnaam is niet importeerbaar en
de toetsen moeten bij de logica kunnen:

| module | wat erin staat |
|---|---|
| `key_rotation.py` | de motor: de ene lees-ontsleutel-versleutel-schrijf-lus, de vindplaatsen, de vingerafdruk, de eindtoets, en de inventaris waar de dekkingsgrendel op hangt |
| `sops_rotation.py` | de ronde over deze repo |
| `project_rotation.py` | de ronde over de projectbestanden, met beide ingangen (sleutel en PAT) |
| `sops_key_secret.py` | het cluster-secret |

Toetsen: `operations-manager/python/tests/test_key_rotation_*.py`,
`test_sops_rotation_round.py`, `test_set_sops_key_secret.py`.

## De geheimenscan

| bestand | doet |
|---|---|
| `scan-secrets.py` | de ingang: een boom, een lijst bestanden, of de hele historie |
| `secret_scan.py` | de regels, en waarom een AGE-kandidaat pas telt als `age-keygen` hem accepteert |

Hangt aan drie plekken: de `secret-scan`-hook in `.pre-commit-config.yaml`, de job `secret-scan`
in `.github/workflows/security.yml`, en `tests/test_secret_scan.py`.

De hele historie nalopen is een eigen gang, en die duurt lang:

```bash
python3 scripts/scan-secrets.py --history                       # deze repo
python3 scripts/scan-secrets.py --history --tree <andere repo>  # een andere clone
```

**De bevindingen van de scanner gaan nooit naar git.** Niet als document, niet als bijlage,
ook niet samengevat tot aantallen en paden: dat is een routekaart naar geheimen die nog
geldig zijn zolang de opruiming loopt, en deze repo gaat naar GitHub. Een scanner die zijn
eigen uitkomst commit maakt het probleem groter dan het was. Ze horen op een intern kanaal.

Het script dwingt dat af door niets anders te kunnen: `report()` schrijft naar stdout en er
is geen vlag die de uitkomst in een bestand zet. Wil je hem bewaren, leid hem dan zelf om
naar een pad BUITEN de repo. `tests/test_secret_scan.py` pint dat vast, zodat zo'n vlag er
niet ongemerkt bij komt.

Dit gaat alleen over de bevindingen. De inventaris van WAAR cryptografie wordt toegepast --
de vindplaatsenlijst in `features/sops-sleutel-vervangen.md` die de rotatieronde omzet --
hoort juist wel in git: BIO2 8.24.01 vraagt om die registratie.

## De rest

| bestand | doet |
|---|---|
| `project_decrypt.py` | schrijft een projectbestand uit met alles ontsleuteld, om te kunnen nagaan hoe een project is ingericht |
| `generate_probe_spec.py` | genereert de probe-spec die `test_probe_spec_drift.py` vastpint |
| `orphan_deployments.py` | vindt deployments zonder project |
| `build-preflight.sh` | weigert een bouw te starten met te weinig vrij geheugen |
| `pod-resources.sh`, `resourcequota-compare.sh` | resourceoverzichten uit het cluster |
| `renew-sandbox-cert.sh`, `certbot/` | het wildcard-certificaat voor de sandbox vernieuwen |
| `sandbox-forward.sh`, `traffic-generator.sh` | hulpjes rond een lopende sandbox |

## Aanroepen

Vanaf de repowortel, en dat is niet vrijblijvend: de vier rotatie-ingangen importeren `opi`, dus
ze draaien alleen in de omgeving van OPI. `python3 scripts/rotate-sops-key.py` stopt op
`ModuleNotFoundError: No module named 'pydantic'`. `uv run --project` zet die omgeving eromheen
zonder de werkmap te verplaatsen, zodat de paden in het commando paden vanaf de wortel blijven:

```bash
uv run --project operations-manager/python python scripts/rotate-sops-key.py --dry-run
```

Daarom dragen ze geen shebang en geen x-bit: er is geen interpreter die het alleen af kan, en een
`./scripts/rotate-sops-key.py` die dat wel belooft zou stranden.

`scan-secrets.py` is de uitzondering. Die importeert `opi` niet en draait op een kale Python 3
(dat is wat de CI-job en de pre-commit hook doen), dus die heeft de shebang wel:

```bash
python3 scripts/scan-secrets.py
```

Dat "kale" is de reden dat hier een eigen `.ruff.toml` staat. De config van OPI staat op
`target-version = "py314"`, en met die stand SCHRIJFT `ruff format` een `except (A, B):` om naar
de PEP 758-vorm `except A, B:`. In `opi/` is dat prima -- daar is de interpreter gepind -- maar
`scan-secrets.py` draait op de `python3` die de ontwikkelaar toevallig heeft, en op 3.13 en ouder
is die vorm een SyntaxError die elke commit weigert. `scripts/.ruff.toml` neemt daarom de hele
OPI-config over en zet alleen de target-version omlaag, zodat de formatter de haakjes laat staan.

Let op wat "de hele OPI-config" betekent: die geldt voor de HELE map en niet alleen voor
`scan-secrets.py`. Elk bestand hier wordt dus beoordeeld met de strenge regelset van OPI, ook een
script dat er al stond. Dat is waarom `orphan_deployments.py` een `# noqa: S603`/`S607` draagt op
zijn git-aanroep, net als de rotatiemodules hiernaast. Geen enkele poort draait `ruff` over deze
map -- de pre-commit hook staat op `^operations-manager/python/`, en de ruff-stap in CI op `opi/`
en `tests/` -- dus draai hem hier met de hand:
`cd operations-manager/python && uv run ruff check ../../scripts/`.
