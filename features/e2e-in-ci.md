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

## Een rode geschudde run lezen

Twee valkuilen, allebei tegengekomen bij het beoordelen van deze suite.

**1. Elke run een andere valler betekent niet "een test laat rommel achter".** Zoek dan
niet in de projectbestanden maar naar iets dat de hele app tegenhoudt. Gemeten geval, en
precies dit patroon: `KubectlConnector.__init__` doet een **blokkerende**
`subprocess.run(["kubectl", "auth", "whoami"], timeout=10)` op de thread die hem bouwt --
in deze opzet de uvicorn-eventloop die elk verzoek bedient. Er is geen cluster, maar op
een machine die wel een `kubectl` heeft (elke dev-box met kind, en de gedeelde dev-server)
faalt die probe niet snel: hij hangt de volle 10 seconden en alles wat op dat moment
onderweg is wacht mee. De connector is een singleton, dus dat gebeurt precies een keer --
bij die ene test die er toevallig naast valt, elke schudbeurt een andere, en de buurtest
verloopt zijn eigen 10s-wachttijd mee.

De reparatie zit in `tests/e2e/conftest.py` (`_keep_kubectl_from_probing`), niet in de
tests die omvielen. Let op waarom hij daar staat en niet in `create_test_app`: de
root-conftest heeft een autouse `reset_kubectl_singleton` die `_instance` bij ELKE test op
None zet, dus eenmalig bij het opstarten stubben houdt geen stand. `tests/e2e/test_no_kubectl_probe.py`
bewaakt allebei de helften.

Wisselt de valler per run en is er geen zulke gedeelde blokkade, kijk dan naar vaste
wachttijden en korte timeouts. Meet het verschil door hetzelfde zaad te herhalen: dezelfde
valler bij hetzelfde zaad is een koppeling, een andere valler is belasting.

**1b. Een test die de gedeelde staat wijzigt, zet die terug in een `finally` die ook de
SCHRIJFACTIE omsluit.** Zelfde gemeten geval: `test_saves_description_change` had het
opslaan buiten zijn `try` staan. Toen dat opslaan zijn wachttijd overschreed -- terwijl de
server het wel degelijk uitvoerde -- liep het herstel nooit, en viel
`test_detail_page_renders` om op een omschrijving die deze test had overschreven. Dat leest
als een niet-verwante volgordefout en is het niet.

**2. `CSRF check failed: token missing` in het log is geen aanwijzing.** Die regel hoort
er precies een keer in te staan en komt uit `test_csrf_browser.py::test_post_without_csrf_token_is_rejected`,
die hem opzettelijk uitlokt en slaagt. `log_cli` drukt live af, dus in een geschudde run
staat hij naast een willekeurige buur. Kijk in welk `live log call`-blok hij valt voordat
je hem als oorzaak aanmerkt.

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
