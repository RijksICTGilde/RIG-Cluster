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

---

## Uitvoering (RC-125, PR #122)

**Gemeten oorzaak: er loopt niets over.** `auth_page`/`authenticated_context` staan per test
(verse browsercontext, eigen koekjespot, dus een eigen wizardtoken); de serverkant is tussen
de twee tests ongewijzigd (met een probe rond elke test gemeten: projectregister,
e-mailtoelating, wizardstaatbestanden); en met de besmetting bewust aangezet -- twee
wizardgangen achter elkaar op dezelfde pagina en in dezelfde context -- begint de tweede
gang alsnog leeg.

De isolatie loopt daarbij langs TWEE wegen: de indiening van een gang wist de wizardstaat
zelf (`clear_wizard_state`, `opi/web/router_wizard.py`), en het openen van de volgende gang
wist hem nog eens via `/forms/wizard/restart` in `open_create_wizard`.

Wat wel omviel: korte vangnetten (10 s) op wachtregels die een VOLLEDIGE serverronde moeten
dekken, plus een vaste `wait_for_timeout(600)` die volgens de meting nooit iets afdekte
(elke cascaderende keuze geeft precies twee `htmx:afterSettle`, de laatste 9-52 ms VOOR de
bestaande voorwaarde, ook op een machine op een kern). Belasting dus, geen koppeling -- en
daarom sprong het rood heen en weer tussen de twee tests in plaats van altijd de tweede te
raken.

**Gedaan:** vangnet naar 30 s met de reden erbij dat het een vangnet is, sprekende meldingen
op de wachtregels (veld, waarde, wat de lijst wel bood), `wait_for_htmx_quiet` in plaats van
de vaste sleep, een isolatiepoort die een achtergebleven regel zelf meldt, de `xfail` en de
stale "open bevinding" eruit, en
`test_a_second_wizard_in_the_same_browser_session_starts_without_a_rule` erbij die de
isolatiebewering vastlegt langs de weg die hem waarmaakt. De twee beweringen zijn niet
samengevoegd.

**Rework (review r1).** De nieuwe test bewaakte die weg eerst niet: hij liet de eerste gang
INDIENEN, en de indiening wist de wizardstaat zelf, dus met de restart eruit bleef hij groen.
De eerste gang stopt nu zodra de regel staat (en toetst dat de staat er dan ook echt is), zodat
de restart het enige overgebleven mechanisme is. Negatieve controle daarna zelf gedraaid: met
de `goto("/forms/wizard/restart")` uit `open_create_wizard` valt de test om op "Wizard is on
step 'Cross-domain toegang', expected 'Projectgegevens'"; met de restart erin is hij groen.
Verder vertaalt `_wacht` nu alleen nog een `TimeoutError` naar een sprekende melding -- een
echte JS-fout gaat ongemoeid door in plaats van als time-out te verschijnen.

**Uitkomst:** tien opeenvolgende runs van het bestand groen (`3 passed`, ~33 s per run),
zonder `xpassed`/`xfailed`. De twee volledige `-m e2e`-runs zijn op verzoek afgebroken: die
kosten ~50 minuten per keer (521 browsertests, elk een echte pagina) en stonden bij het
afbreken op 24/521 zonder rood.
