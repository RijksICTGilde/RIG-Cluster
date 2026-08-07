# E2E in de vaste loop, in willekeurige volgorde

## Wat het is

De lokale E2E-laag (`tests/e2e/`, marker `e2e and not sandbox`) draait als eigen job in
`.github/workflows/ci.yml`, bij elke PR en bij push naar main. Hij draait daar niet in
bestandsvolgorde maar **geschud**, met elke run een nieuw zaad.

## Waarom

De gewone testjob sluit de marker uit (`-m "not e2e"`). Zonder eigen job draaide deze laag
dus alleen als iemand er met de hand om vroeg -- en dat is precies hoe een breuk erin
wekenlang onopgemerkt kan blijven. De laag vangt dingen die de unit-tests structureel niet
zien: wat de gerenderde pagina werkelijk doet.

Waarom geschud en niet in bestandsvolgorde: de suite is vandaag aantoonbaar
volgorde-onafhankelijk (gemeten: twee volledige geschudde runs groen). Dat is een
eigenschap die stil wegzakt zodra iemand een test schrijft die op de vorige leunt. Een
suite die alleen groen is in de volgorde waarin de bestanden toevallig heten, is niet
geisoleerd maar bofferig. Door elke CI-run een andere volgorde te laten samplen, komt een
nieuwe koppeling boven als een falende run in plaats van over maanden.

Een omgekeerde *bestands*volgorde is geen vervanging: die houdt de tests binnen een bestand
nog steeds bij elkaar en op volgorde -- precies de aanname die een lekkende test maakt.
`pytest-randomly` schudt allebei.

## Gebruik

```bash
task test-e2e                      # normale volgorde
task test-e2e-random               # geschud (bestanden en de tests daarbinnen)
task test-e2e-random SEED=12345    # speel een specifieke schudbeurt terug
```

Een rode geschudde run is na te spelen omdat `pytest-randomly` het zaad in de
report-header afdrukt: `Using --randomly-seed=<n>`. Neem dat getal over in `SEED=<n>`.

## Configuratie

| Onderdeel | Waar |
|---|---|
| CI-job | `.github/workflows/ci.yml`, job `e2e` |
| Taak | `Taskfile.yaml`, `test-e2e-random` |
| Standaard uit | `-p no:randomly` in `addopts` (`operations-manager/python/pyproject.toml`) |

`pytest-randomly` staat standaard **uit**. Eenmaal geinstalleerd schudt hij namelijk
alles, en dan zouden ook de ~6300 unit-tests bij elke run van volgorde wisselen als
neveneffect van een wijziging die over de E2E-laag gaat. `task test-e2e-random` en de
CI-job zetten hem aan met `-p randomly`.

**Geen `-q` op een geschudde run.** De `addopts` bevatten al `-v -q` (verbosity 0); nog een
`-q` komt op -1 uit, en bij -1 laat pytest de report-header weg -- inclusief de regel met
het zaad. Dan is een rode run niet meer na te spelen. Controleer met
`... -p randomly --collect-only | grep randomly-seed`: dat hoort een regel te geven.

## Afhankelijkheden

- `pytest-randomly` in de test-groep. Geen wijzigingen aan applicatiecode: dit zit
  volledig in de testopzet en de CI-configuratie.
