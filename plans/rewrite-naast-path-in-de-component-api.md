# `rewrite` naast `path` in de component-API

Status: plan, 12 augustus 2026. Antwoord op de terugvraag van zad-cli in `plans/vragen-uit-zad-cli.md`, punt 1.

## De vraag, en waarom hij klein is

Bij punt 1 van de doorlopen bleek een niet-root `path` te werken zoals bedoeld, maar zonder herschrijving: het pad komt ongewijzigd bij de container aan. Wij boden aan het veld toe te voegen; zad-cli heeft daarop geantwoord dat ze het willen, en heeft scherp opgeschreven wat wel en niet.

> Wat wij nodig hebben is het kleine veld. Een `rewrite` naast `path`, allebei losse strings, en jullie maken er `[{"match": path, "rewrite": rewrite}]` van zoals je nu `[{"match": path}]` maakt. Meer niet.

Hun aanleiding is de standaardvorm van een image dat je niet zelf schrijft: `zad component add --path /api` betekent voor iedereen die het intikt dat het component extern onder `/api` hangt terwijl de applicatie erin op `/` luistert. Zonder herschrijving is `--path` daarvoor onbruikbaar.

## Wat er al ligt

Alles behalve de API-kant. Dat maakt dit een doorgeefklus en geen ontwerpklus.

| Laag | Stand |
|---|---|
| Projectbestand | `$defs/component-path` kent `match` én `rewrite`, allebei met dezelfde tekenvalidatie |
| Manifest | `manifests/ingress.yaml.jinja:63-65` rendert de herschrijving; `path: [{match: /api, rewrite: /}]` levert `rewrite "^/api/?(.*)$" "/$1" break;` |
| Formulierlaag | `COMPONENT_PATH_REWRITE_EDITABLE` bestaat (`forms/editables/fields/components.py:127`) en zit in de padgroep naast `COMPONENT_PATH_MATCH_EDITABLE` |
| API | **ontbreekt**: `ADD_COMPONENT_VALIDATORS` kent alleen `"path"` (`api/validation.py:63`) |

De omzetting naar de lijstvorm staat op één plek, `utils/project_utils.py:260`:

```python
"path": [{"match": path}],
```

Dat is de regel die een tweede sleutel moet kunnen dragen.

## Wat er moet gebeuren

**Fase 1: het veld door de API.** `rewrite` erbij in `ADD_COMPONENT_VALIDATORS` en in de update-kant, op `COMPONENT_PATH_REWRITE_EDITABLE` zodat de validatie dezelfde is als in het formulier en er geen tweede regel ontstaat. Het request-model krijgt het veld, de payload draagt het mee, en `project_utils.py:260` zet er `{"match": path, "rewrite": rewrite}` van wanneer het gezet is.

**Geen standaardwaarde.** Dat is expliciet gevraagd en het is ook het juiste gedrag: `rewrite` weglaten betekent dat het pad ongewijzigd doorgaat, precies zoals nu. Een impliciete `/` zou bestaande componenten van gedrag laten veranderen, en voor een component dat zijn eigen prefix afhandelt is dat verkeerd. De sleutel hoort dus afwezig te blijven in het projectbestand als de aanroeper hem niet meestuurt, niet aanwezig met een lege waarde.

**Fase 2: de beschrijving.** De veldomschrijving van `path` in de API zegt sinds de vorige ronde dat een niet-root pad ongewijzigd doorgestuurd wordt. Die tekst krijgt er de tegenhanger bij: met `rewrite` gebeurt dat wel, met het `/api` naar `/`-voorbeeld erbij. Eén zin die het verschil zegt, want dat is precies waar zad-cli in liep.

Verifieerbaar, en meet het op het gegenereerde manifest en niet op de code:

1. `POST .../components` met `path: /api` en zonder `rewrite` levert een projectbestand met `path: [{match: /api}]` en een ingress zonder herschrijfregel, byte-identiek aan vandaag.
2. Dezelfde aanroep mét `rewrite: /` levert `path: [{match: /api, rewrite: /}]` en een ingress met `rewrite "^/api/?(.*)$" "/$1" break;`.
3. `PATCH` van alleen `rewrite` op een bestaand component laat `match` staan, en andersom.
4. Een `rewrite` met tekens buiten het toegestane patroon wordt geweigerd met dezelfde melding als in het formulier.

## Wat er bewust niet in zit

**De samengestelde vorm.** `path` als lijst van objecten, met meerdere paden per component, is uitdrukkelijk niet gevraagd: "wacht daar dit veld niet op". Het projectbestand kan het al, dus wie het later wil kan het toevoegen zonder dit werk over te doen.

**Geen migratie.** Bestaande projectbestanden blijven zoals ze zijn; een pad zonder `rewrite` betekent nog steeds ongewijzigd doorsturen.

## Waar op te letten

**Twee schrijfwegen, één regel.** De API en het formulier moeten dezelfde validatie op `rewrite` gebruiken. Daarom `COMPONENT_PATH_REWRITE_EDITABLE` hergebruiken en er geen tweede patroon naast zetten; dat is dezelfde afspraak die de rest van `api/validation.py` al volgt.

**Afwezig is niet leeg.** Als een aanroeper `rewrite` niet meestuurt, hoort de sleutel niet in het projectbestand te verschijnen. Een lege string zou door het schema komen en in het sjabloon als een echte herschrijving naar de wortel kunnen uitpakken, en dat is precies het gedrag dat niemand gevraagd heeft.
