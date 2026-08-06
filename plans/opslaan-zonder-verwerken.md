# Opslaan zonder verwerken

Status: plan, 7 augustus 2026. Niet gebouwd. Aanleiding: in de opbouwfase van een project wil je tien dingen achter elkaar toevoegen en één keer verwerken, niet tien keer een volledige uitrol uitlokken.

Dit plan is gezocht in `zad-cli` en staat daar niet: de enige treffers op "refresh" gaan daar over auth-sleutels en `gh auth refresh`.

## Wat er nu is, gemeten

**De wizard kent het idee al, de API niet.** Een formuliersectie kan `post_save_action="save_only"` dragen, en invite gebruikt dat met een expliciete reden: *"editing an invite changes no manifests, so it does not trigger a deploy"*. Eén dienst van de negentien.

Aan de API-kant is er geen enkele weg. De v2-router maakt **twaalf** taaktypes aan, en de handlers roepen op **tien** plekken `process_project_from_git` aan, verdeeld over vier modules. Er is precies één ontsnapping en die is niet de bedoelde: als een `clear` niets te verwijderen vond, blijft verwerken achterwege omdat het bestand niet veranderde.

```
12 taaktypes in de v2-router
10 aanroepen van process_project_from_git in de handlers
 4 handler-modules die verwerken
 1 dienst die save_only gebruikt (invite)
```

**De CLI hangt eraan.** `zad-cli` roept deze endpoints aan en houdt een kopie van de spec in `api/upstream-openapi.json`. Een vlag die alleen in OPI bestaat, bereikt de gebruiker daar niet.

## Wat het moet worden

Een vlag op elk endpoint dat normaal verwerkt: schrijf de wijziging weg in het projectbestand, sla de verwerking over. Daarna verwerk je in één keer. Dat laatste bestaat al als `refresh_project`.

## Het echte vraagstuk is niet de vlag, het is de drift

De vlag zelf is klein. Wat hem gevaarlijk maakt is dat het projectbestand daarna voorloopt op het cluster, en dat niemand ziet hoe lang al. Stille drift is erger dan een trage uitrol, want een trage uitrol merk je.

Wat er dus bij hoort:

1. **Zichtbaar maken dat er onverwerkte wijzigingen liggen.** Op de projectdetailpagina en in de CLI. Zonder dit is de vlag een manier om je project stilletjes uit de pas te laten lopen.
2. **Weten sinds wanneer.** Een commit die niet verwerkt is, is iets anders dan tien commits van een week oud.
3. **Eén knop om alsnog te verwerken**, op de plek waar je de drift ziet.

## Voorstel

1. **Eén vlag, op één plek bepaald.** De endpoints verschillen, maar de vraag is overal dezelfde. Laat de vlag door de taak-payload reizen en door de handlers gelezen worden op de plek waar ze nu `process_project_from_git` aanroepen, in plaats van hem per endpoint na te bouwen. Tien aanroepen, één regel.

2. **Standaard blijft verwerken.** Wie niets meegeeft, krijgt het gedrag van vandaag. Dit is een uitzondering die je aanvraagt, geen instelling die je vergeet.

3. **De taakuitkomst zegt wat er niet gebeurd is.** Het antwoord op zo'n aanroep hoort te melden dat er niet verwerkt is, zodat een script het weet zonder de documentatie te lezen. Het bestaande `processing_status: "skipped"` is daar het aanknopingspunt.

4. **Drift zichtbaar in de UI en in de CLI**, met de drie punten hierboven. Dit is geen bijzaak: zonder dit levert de vlag meer problemen op dan hij oplost.

5. **De CLI krijgt hem mee**, inclusief de spec-kopie in `api/upstream-openapi.json`, anders bestaat de vlag alleen op papier.

## Volgorde

1. De vlag door de payload en door de tien aanroepen, met de standaard ongewijzigd. Verifiëren: bestaande aanroepen gedragen zich identiek, en met de vlag verschijnt er geen verwerking in de taakstappen.
2. De uitkomst die het meldt, met een test op het antwoord.
3. Drift zichtbaar maken: de detailpagina eerst, want daar kijkt een gebruiker.
4. De CLI, met de bijgewerkte spec.

## Waar op te letten

**Niet elke overslag is veilig.** Sommige wijzigingen zijn alleen betekenisvol ná verwerking (een nieuw component dat nog geen namespace heeft, een dienst die iets moet provisioneren). Loop de twaalf taaktypes langs en bepaal per stuk of overslaan mag. Waar het niet mag, weiger de vlag met een uitleg in plaats van hem stil te negeren.

**Wizard en API horen hetzelfde te doen.** `post_save_action="save_only"` bestaat al aan de formulierkant. Bouw er geen tweede mechanisme naast; als de vlag er is, hoort save_only erop te leunen.

**Dit raakt de plannen die nu lopen.** RC-43 verbouwt hoe een wizard basis en mutaties bijhoudt en RC-44 raakt de opslagstap. Deze vlag zit in de laag daaronder (de taakhandlers), maar kijk bij het samenvoegen naar `post_save_action`, want daar komen ze samen.

**Meet de drift voor je hem toont.** "Onverwerkte wijzigingen" moet ergens uit af te leiden zijn: de laatste verwerkte commit tegen de laatste commit in de projects-repo. Controleer dat die informatie bestaat en betrouwbaar is voordat de UI hem als waarheid presenteert.
