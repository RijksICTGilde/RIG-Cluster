# Audit: zoeken, op een periode filteren en bladeren

Het tabblad Audit (`/projects/<naam>/audit`) toont nu de nieuwste 50 rijen (tot 200 met `?limit=`) en verder niets. Je kunt niet terugbladeren, niet zoeken en niet op een periode filteren. Daardoor heb je er in een project met wat geschiedenis weinig aan. De vragen die je er in de praktijk mee stelt zijn "wat deed persoon X" en "wat gebeurde er tussen datum A en B".

Deze taak voegt drie dingen toe: één zoekveld dat over alle zichtbare velden zoekt (ook over de persoon), een periode (van, tot en met) en bladeren met **Nieuwer** en **Ouder**. Bewust eenvoudig: geen aparte filters per kolom en geen genummerde pagina's. Die kunnen later.

Alle nieuwe namen hieronder zijn een VOORSTEL, tenzij er een codeanker bij staat. Wijk je af, zeg het in de PR.

## Gemeten

Op main `393bc7741` (gelijk aan forgejo/main op 30-10-2026):

- `list_actions(project, *, limit, before)` in `opi/services/audit.py:178` bladert alleen op `time` (`NotificationEvent.time < before`) en sorteert op `(time DESC, id DESC)`. Liggen twee rijen op hetzelfde tijdstip en valt de paginagrens daartussen, dan valt er een weg. `id` is een willekeurige UUID (`gen_random_uuid()`), dus die dient alleen als tiebreaker voor een stabiele volgorde.
- De pagina gebruikt `before` niet. `project_audit` (`opi/web/router.py:1382`) rendert via `render_project_page`. Daar haalt `opi/web/router.py:1934-1942` de rijen op met `list_actions(project_name, limit=audit_limit(request))` en zet ze om met `build_lotc_audit` (`opi/web/lotc_switch.py:801`).
- Template: `opi/templates_lotc/bg/project-tabs.html.j2:1071` (`active_tab == 'audit'`).
- Index: `idx_notification_events_project` op `(project, time DESC)` (`opi/services/persistence/notification_events.py:61`). Die beperkt elke zoekopdracht al tot één project, en de bewaarlus verwijdert rijen na een jaar. Een trigram- of full-text-index is dus niet nodig.
- Het zoekpatroon bestaat al op de deploymentlijst (`project-tabs.html.j2:285-305`) en de projectenlijst (`bg/projects.html.j2:80-110`): een `<form>` met `hx-get`, `hx-trigger="input delay:300ms, search, submit"`, `hx-select`, `hx-push-url="true"`, `<c-search-field name="q">` en een `nldd-toolbar` als kale markup. Neem dat patroon over, inclusief de uitleg waarom het `nldd-toolbar` is en niet `c-toolbar`.
- Voor datums heeft LOTC `<c-date-field>`: de waarde is ISO (`yyyy-mm-dd`) en het scherm toont de Nederlandse notatie. Er staat ook een eigen widget in `opi/templates_lotc/widgets/date.html.j2`. Lees die en kies er één. Zeg in de PR welke.
- Tests draaien tegen Postgres (`orm_db` in `tests/conftest.py:677`), dus `ILIKE` en JSONB-operatoren zijn in een test te gebruiken. Bestaande tests: `tests/test_audit.py` (onder meer `test_list_actions_pagineert_op_time`) en `tests/test_audit_tabblad.py` (route, guard, `audit_limit`).
- De tijden in de tabel worden NIET omgezet: `rij.time.strftime(...)` op een `timestamptz` uit de database. Meet in de sandbox of dat UTC toont. Zie stap 4.

## Wat er moet komen

### 1. `list_actions`: zoeken, periode en een cursor op `(time, id)`

Nieuwe signatuur (voorstel):

```python
async def list_actions(
    project: str,
    *,
    limit: int = LIST_DEFAULT_LIMIT,
    query: str | None = None,
    since: datetime | None = None,   # inclusive
    until: datetime | None = None,   # exclusive
    before: AuditCursor | None = None,
    after: AuditCursor | None = None,
) -> list[NotificationEvent]:
```

- **Zoeken** (`query`): hoofdletterongevoelig `ILIKE '%…%'`, waarbij `%`, `_` en `\` in de invoer ge-escaped worden (`escape="\\"`). Het is één term: de invoer gaat na `strip()` als geheel in het patroon, zonder hem op te splitsen in woorden. Een treffer in een van deze velden telt:
  - `actor`, `actor_kind`
  - `summary` (bevat al het subject, de action, de rol en de cause)
  - `deployment`
  - `data->>'action'`, `data->>'resource_path'`, `data->>'cause'`, `data->>'grant'`
  - `(data->'commit_shas')::text` en `(data->'fields')::text`

  **Niet** `data::text` als geheel: dan geeft zoeken op "allowed" of "action" elke rij terug. **Niet** `origin`: die kolom is alleen voor platformbeheerders (`features/audit.md`, "Wat je ziet"). Zou je op het IP kunnen zoeken, dan kan een projectbeheerder met een zoekopdracht nagaan welk IP-adres bij een handeling hoorde. Leg dat vast in een test (stap 5).
- **Periode**: `since <= time < until`. De route rekent de datums om (stap 2).
- **Cursor**: `before` en `after` zijn een `(time, id)`-paar. Gebruik een rijvergelijking (`tuple_(time, id) < tuple_(t, i)`), zodat rijen met hetzelfde tijdstip niet wegvallen. `after` vraagt oplopend op met `limit` en draait de lijst daarna om, zodat het resultaat altijd nieuwste-eerst is. `before` en `after` samen is een programmeerfout: gooi een `ValueError`.
- De oude `before: datetime` vervalt. Er is geen andere aanroeper dan de route (gecontroleerd met grep); werk de docstring en het voorbeeld in `features/audit.md` bij.
- Het project blijft uit de aanroep komen, nooit uit een filter. `list_actions` controleert zelf niets (`features/audit.md`).

### 2. De route leest de parameters

In `opi/web/lotc_switch.py`, naast `audit_limit`, één functie die de querystring leest (voorstel `audit_filters(request) -> AuditFilters`, een kleine dataclass):

| param (voorstel) | wat |
|---|---|
| `q` | zoekterm, afgekapt op 200 tekens; leeg betekent geen filter |
| `from` | `yyyy-mm-dd`, het begin van die dag in Europe/Amsterdam |
| `until` | `yyyy-mm-dd`, inclusief: het begin van de dag ERNA in Europe/Amsterdam, zodat "tot en met 30-10" de hele dag 30 oktober meeneemt |
| `before` / `after` | de cursor, als tekst `<iso-time>_<uuid>` (voorstel) |

- Een ongeldige waarde (geen datum, een kapotte cursor) geeft geen fout en geen 500, maar laat dat filter weg. Dat is hetzelfde gedrag als `audit_limit`. De pagina toont dan geen filter dat niet werkt.
- `from` na `until`: geef gewoon een lege lijst. Draai de datums niet stilzwijgend om.
- De tijdzone is Europe/Amsterdam, net als `AUDIT_RETENTION_HOUR`. Zoek uit of er al een constante of helper voor die zone bestaat (`opi/utils/`, `opi/core/config.py`) en gebruik die.
- `limit` blijft zoals hij is.
- Het blok in `render_project_page` (`router.py:1934-1942`) geeft de filters door aan `list_actions` en zet in de context wat de template nodig heeft: de huidige filterwaarden, en de cursor voor Ouder en Nieuwer.
  - **Ouder** verschijnt alleen als er een volgende pagina is. Vraag daarvoor `limit + 1` rijen op en toon er `limit`.
  - **Nieuwer** verschijnt alleen als je niet op de nieuwste pagina zit, dus als de pagina met een `before` of `after` is opgevraagd. Een `after`-pagina die zelf de nieuwste blijkt te zijn, mag de knop toch nog tonen. Een klik geeft dan een lege lijst of dezelfde rijen. Dat is acceptabel; zeg het in de PR als je het anders oplost.
  - Bladeren houdt `q`, `from`, `until` en `limit` vast. Een nieuwe zoekopdracht of een nieuwe periode wist de cursor.

### 3. De template

In `project-tabs.html.j2` bij `active_tab == 'audit'`, binnen het bestaande `panel("Audit", ...)`:

- Een formulier volgens het patroon van de deploymentlijst. De actie is de URL van het auditblad (`tabs['audit'].url`, of hoe die daar heet), en het gaat via GET met `hx-get`, `hx-select` en `hx-target` op een omhullende id (voorstel `audit-lijst`), plus `hx-push-url="true"`. Een gefilterde of gebladerde pagina is dan een link die je kunt delen.
- In de toolbar:
  - het zoekveld `q` met placeholder "Zoek op persoon, actie, deployment of commit", een `accessible-label` en een eigen `id` voor het focusherstel (zie het commentaar bij `projects-zoekveld` in `bg/projects.html.j2`)
  - twee datumvelden met de labels "Van" en "Tot en met"
- Onder de tabel de knoppen **Nieuwer** en **Ouder** als links (`c-link` of `c-button` met `href`). Ze werken ook zonder JavaScript. Let op het commentaar over `c-button` en attribuut-JSON: gebruik een gewone `href` met een querystring en geen attribuut-JSON.
- Een lege uitkomst met een actief filter toont "Geen gebeurtenissen gevonden voor deze zoekopdracht" en een link om de filters te wissen. De bestaande melding "Nog niets vastgelegd" blijft alleen staan voor een project zonder rijen en zonder filter.
- Een korte hint bij het zoekveld of in de `info_text` van het paneel: na 90 dagen is de persoon uit een rij verwijderd (bewaartermijn, `features/audit.md`). Zoeken op een persoon vindt dus alleen de laatste 90 dagen.

### 4. Tijden in Nederlandse tijd

De periode rekent in Europe/Amsterdam. Toont de tabel UTC (meet dat), dan sluit de periode niet aan op wat je ziet: een handeling om 00:30 Nederlandse tijd staat in de tabel op de dag ervoor. Zet in dat geval `time` in `build_lotc_audit` om naar Europe/Amsterdam, en voeg een test toe die laat zien dat het tijdstip in die zone staat. Toont de tabel al Nederlandse tijd, sla deze stap dan over en zeg dat in de PR.

### 5. Tests

In `tests/test_audit.py` (service, tegen Postgres):

- Zoeken vindt een rij op elk veld uit de lijst van stap 1, telkens met een eigen rij en een eigen term. Gebruik waar het kan één geparametriseerde test.
- Zoeken is hoofdletterongevoelig.
- `%` en `_` in de zoekterm zijn letterlijke tekens: "a_b" vindt "axb" niet.
- Zoeken op een waarde die alleen in `origin` staat, vindt de rij NIET.
- Zoeken op "allowed" of "action" vindt niet elke rij. Dit bewaakt dat `data::text` als geheel niet doorzocht wordt.
- Een zoekterm die in een ander project staat, geeft geen rij uit dat project.
- Periode: rijen net voor `since`, op `since`, net voor `until` en op `until`. Alleen de middelste twee komen mee.
- Cursor: vijf rijen, waarvan drie met exact hetzelfde `time`. Blader met `limit=2` met `before` naar het eind en met `after` terug. Elke rij komt precies één keer langs, in dezelfde volgorde. Dit vervangt `test_list_actions_pagineert_op_time`.
- `before` en `after` samen geeft een `ValueError`.

In `tests/test_audit_tabblad.py` (route en parsing):

- `audit_filters`: een geldige en een ongeldige datum, `until` die de hele dag meeneemt (de grens op 23:59 en 00:00 Europe/Amsterdam, ook rond de wisseling naar zomertijd of wintertijd), een kapotte cursor die genegeerd wordt, en `q` die afgekapt wordt.
- De route met `?q=…&from=…&until=…` geeft 200 en alleen de bedoelde rijen. De Ouder-link draagt de filters en een cursor. Zonder `audit.read` blijft het een 403: de guard verandert niet.

Draai gericht met `--timeout`, bijvoorbeeld `uv run pytest tests/test_audit.py tests/test_audit_tabblad.py -x -q --timeout 120`. Geef geen eigen `-m` mee, want die vervangt de `addopts`-selectie uit `pyproject.toml`.

### 6. Documentatie

In `features/audit.md`, paragraaf "Wat je ziet" en "Hoe je het gebruikt":

- zoeken: welke velden wel en welke niet, en waarom `origin` niet
- de periode, in Europe/Amsterdam en inclusief de einddatum
- bladeren met een cursor op `(time, id)`, en waarom geen offset
- de bewaartermijn van 90 dagen voor de persoon
- het voorbeeld van `list_actions` met de nieuwe parameters

## Wat wordt schrijfbaar, welke guard

Niets wordt schrijfbaar. Dit is alleen leeswerk.

De guard op het lezen blijft `Action.AUDIT_READ` op de route. Het project komt uit het pad, nooit uit `q` of een andere parameter. De nieuwe parameters zijn alle vier gebonden SQL-parameters, met de zoekterm ge-escaped voor `ILIKE`. Wat een lezer extra kan zien: alleen rijen van zijn eigen project, die hij nu al met `?limit=` kon opvragen. Plus de mogelijkheid om te testen of een waarde voorkomt in `data.fields` en de commit-SHA's. Die velden mogen geen persoonsgegevens dragen (`features/audit.md`, `tests/test_audit_hook.py`). `origin` is daarom bewust niet doorzoekbaar.

## Afspraken

- Code in het Engels: namen, testnamen, commentaar en docstrings. Ook waar de module eromheen Nederlands is. Zet bestaand Nederlands commentaar niet om.
- Docs (`features/audit.md`) en UI-teksten in het Nederlands.
- Geen em dashes, nergens.
- Imports bovenaan het bestand. Daarna `uv run ruff check . --fix`, `uv run ruff format .` en `uv run pyright` in `operations-manager/python`.

## Buiten scope

- Aparte filters per kolom (uitkomst, soort actor, deployment) en een keuzelijst met personen. Het zoekveld dekt dat voorlopig.
- Genummerde pagina's of een totaal aantal.
- Exporteren (CSV) van de audit.
- Een audit op platformniveau, over alle projecten heen.
