# Rechten, taak 2: de audit-opslag en de bewaarlus

Opdracht: bouw de tabel waarin ZAD vastlegt wie wat deed, de functie die erin schrijft, een functie die eruit leest, en de lus die persoonsgegevens na 90 dagen weghaalt en rijen na een jaar opruimt. Nog niemand roept de schrijffunctie aan; dat doet taak 3. Dit verandert geen gedrag.

**Lees eerst `plans/rechten-op-paden-tad-en-wies.md`, paragraaf 6 en 8.** Daar staat wat er wordt vastgelegd en de signatuur van `record_action` (8.2), die vast is omdat taak 3 hem aanroept. Het datamodel komt uit `features/futures/gebeurtenissen-vastleggen-en-melden.md`, punt 7 (`notification_events`); de bewaartermijn uit punt 6 van dat document.

Deze taak loopt parallel met taak 1 (`plans/rechten-1-rechtensysteem.md`). Gebruik niets uit taak 1: `Action`, `Decision` en `Resource` bestaan op jouw basis nog niet. Actions en causes zijn hier tekst, met de waarden uit ontwerp 8.1 en 5.3.

## Waarom

"Wie deed wat" staat nu verspreid: de actor als trailer op een commit van een projectbestand, en `async_tasks.created_by`. Er is geen lijst die je kunt doorzoeken, en een weigering wordt nergens vastgelegd. BIO2 8.15.01 vraagt per logregel actie, object, resultaat, oorsprong, actor en tijdstempel.

## Wat er moet gebeuren

### 1. De tabel

`notification_events` precies zoals in punt 7 van het gebeurtenissendocument, met de indexen die daar staan: een model in `opi/services/persistence/` naar het patroon van `async_tasks.py` en `sessions.py`, en een Alembic-migratie in `opi/migrations/versions/` na `011_add_sessions.py`. Alleen invoegen; geen `UPDATE` behalve de lus uit stap 3.

`source` vraagt een OIN die nog niet bestaat. Gebruik een placeholder uit de configuratie en volg regel 1 van de beslechting "CloudEvents als intern opslagformaat" in dat document: zo'n record dient zich niet als CloudEvent aan.

*Bewijs:* de migratie draait op een lege database en terug (`downgrade`); het model komt overeen met punt 7.

### 2. Schrijven en lezen

`opi/services/audit.py` met `record_action` exact volgens ontwerp 8.2: dezelfde naam, dezelfde argumenten, async, gooit niets naar de aanroeper, en telt een mislukte schrijfactie op in een Prometheus-teller naast de bestaande in `opi/core/metrics.py`.

Vul de kolommen zo: `type` is `nl.rig.zad.action.performed.v1` of `nl.rig.zad.action.denied.v1`; `subject` is `<project>` of `<project>/<deployment>`, nooit een e-mailadres; `actor` het e-mailadres of de naam uit `Actor`, `actor_kind` de soort; `origin` het client-IP zoals de aanroeper het geeft; `summary` een korte Nederlandse regel ("demo/live: deployment.delete door ops"); de rest in `data`. `severity` is `informational` bij een toegestane actie en `actionable` bij een weigering. `category` volgt de typen uit deel 1 van de gebeurtenissendocumenten; kies de categorie voor beheer en beveiliging en zeg in de PR welke.

Daarnaast een leesfunctie voor taak 3 (VOORSTEL `list_actions(project, *, limit, before)`), nieuwste eerst, met paginering op `time`.

*Bewijs:* tests op een echte database (het patroon van de bestaande persistence-tests): een aanroep levert één rij met de juiste kolommen; een weigering levert type `denied` met de cause in `data`; `subject` en `summary` bevatten nooit een `@`; een kapotte sessie laat `record_action` niet gooien en verhoogt de teller; `list_actions` geeft alleen rijen van dat project.

### 3. De bewaarlus

Punt 6 van het gebeurtenissendocument: na 90 dagen `actor` en `origin` op `NULL` (niet op een placeholder-tekst), na een jaar de rij weg. Twee passes, op de twee indexen uit punt 7. Een geplande taak naar het patroon van `opi/core/reconciliation_scheduler.py`, één keer per dag.

*Bewijs:* een test met rijen van 89, 91 en 366 dagen oud: na een ronde heeft de eerste nog alles, de tweede geen `actor` en `origin` maar wel de rest, en de derde is weg.

### 4. Documentatie

`features/audit.md`: wat er wordt vastgelegd, waar, hoe lang, en dat de koppeling en het tabblad uit taak 3 komen. Werk `features/futures/gebeurtenissen-plan-van-aanpak.md` bij met een regel dat de tabel er is en de audit zijn eerste schrijver is.

## Wat er buiten valt

- De hook die `record_action` aanroept, en het audit-tabblad: taak 3.
- Alles in `opi/core/authz/`, `opi/api/principaal.py`, de routes en de templates: taak 1.
- Postvak, ontvangers, push-kanalen, export: de rest van de gebeurtenissendocumenten.

## Wat niet mag

- Geen OPI uitrollen. Geen migratie draaien tegen een database in een cluster.

## Verifieerbaar

1. `uv run ruff check . --fix && uv run ruff format . && uv run pyright` schoon in `operations-manager/python`.
2. De tests uit stap 1 tot en met 3 groen, plus de bestaande suite.
3. `record_action` heeft exact de signatuur uit ontwerp 8.2; een test importeert hem en roept hem met alle argumenten als keywords aan.
