# Twee browsertests die elkaar in de weg zitten

In `tests/e2e/test_wizard_cross_domain_policy.py` staan twee tests in `TestTheWizardProducesANetworkPolicy` die elkaar beïnvloeden. Ze zijn de laatste wisselvallige tests die we hebben, en ze zitten op een vervelende plek: dit is het bestand dat de hele keten door de browser bewijst (wizard → projectbestand → NetworkPolicy), en het bestaat juist omdat een eerdere fout daar door alle onderliggende tests heen glipte.

## Wat er gemeten is

Drie keer achter elkaar gedraaid, met exact hetzelfde commando en op exact dezelfde code:

```
uv run pytest tests/e2e/test_wizard_cross_domain_policy.py -m e2e -p no:randomly
```

- run 1: `test_a_rule_filled_in_the_browser_becomes_a_networkpolicy` FAALT, de tweede test `xpassed`
- run 2: idem
- run 3: de eerste test slaagt, de tweede `xpassed`

Eerder op dezelfde dag gaf dat commando "1 passed, 1 xfailed". De uitkomst wisselt dus zonder dat er iets verandert, en de volgorde ligt vast (`-p no:randomly`).

De tweede test draagt al een `xfail(strict=False)` met deze reden:

> Los draait deze test groen; na de test hierboven niet. De twee delen een browsersessie, en sinds de cascade weer werkt vult de eerste test er werkelijk een regel in, waar de tweede mee begint. Dat is isolatie tussen deze twee tests en geen fout in de code.

**Die diagnose is niet compleet.** Hij gaat ervan uit dat de eerste test de tweede besmet. De meting laat zien dat het over en weer gaat: soms valt juist de EERSTE om, terwijl de tweede het dan wél haalt. De markering zit dus op de verkeerde test, of preciezer: er staat een markering waar een reparatie hoort.

## Wat er moet gebeuren

1. **Vaststellen wat er precies overloopt.** De `auth_page`-fixture, een gedeelde browsercontext, achtergebleven wizardstaging op de server, of een project dat de eerste test aanmaakt en de tweede tegenkomt. Meet het; schrijf op wat er lekt en langs welke weg.
2. **Isoleren.** Dat is de reparatie, niet het versoepelen van een assertie en niet een tweede `xfail` erbij. Als isolatie betekent dat elke test zijn eigen browsercontext krijgt, meet dan ook wat dat kost aan looptijd.
3. **De `xfail` weghalen.** Als de tweede test na de reparatie groen staat, hoort die markering weg; hij verbergt dan alleen nog informatie. Blijft hij nodig, dan moet de reden kloppen met wat er werkelijk gebeurt.
4. **Aantonen dat het staat.** Draai dit bestand **tien keer** achter elkaar en meld de uitkomst van elke run. Twee runs bewijzen hier niets: de uitkomst wisselde al binnen drie.

## De valkuil

De verleiding is de tests samen te voegen tot één, want dan kunnen ze elkaar niet meer storen. Doe dat niet zonder te zeggen wat je opgeeft: het zijn twee verschillende beweringen. De eerste zegt dat een mens de stap kan invullen en dat daar een NetworkPolicy uit komt; de tweede dat een tegenpartij die hier niet bestaat óók een policy oplevert, omdat het noemen van een peer hem niets toekent. Die tweede bewering is de reden dat de cross-domain-dienst veilig is; hij mag niet verdwijnen in een grotere test.

## Verifieerbaar

- Tien opeenvolgende runs van `tests/e2e/test_wizard_cross_domain_policy.py`, allemaal groen, met de uitkomsten in de PR.
- `uv run pytest -m e2e -q` twee keer achter elkaar groen.
- Geen `xfail` meer op de tweede test, of een reden die klopt met de meting.

## Wat er buiten valt

- Nieuwe functionaliteit in de cross-domain-dienst.
- De andere e2e-tests; als de reparatie een gedeelde fixture raakt, meld dan wat dat elders doet, maar ga niet de hele suite herbouwen.
