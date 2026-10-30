# Audit: één rij per handeling in de modal-wizard

Eén restore in de sandbox leverde acht keer dezelfde rij `restore.run` op (project `tas-hl5`, 30-10-2026 07:49-07:50, `admin@sandbox.rijksapp.dev`). De afspraak in `features/audit.md` is één rij per beslissing én één rij per handeling. De modal-wizard breekt die afspraak, en dat geldt niet alleen voor restore: ELKE stap van ELKE modal-wizard schrijft een rij, bij een gewone bewerking als `project.edit`.

Alle nieuwe namen hieronder zijn een VOORSTEL, tenzij er een codeanker bij staat. Wijk je af, zeg het in de PR.

## Gemeten

Op main `393bc7741`:

- `_require_flow_access` (`opi/web/router_detail_edit.py:235`) roept `authorize(...)` aan met de action uit `FLOW_ACTIONS`. Voor `modal-restore` is dat `restore.run`, voor `modal-backup` `backup.create`, voor `modal-edit-team` `member.manage` en voor elke andere stroom `project.edit`. Elke `authorize` gaat via `record_decision` (`opi/core/authz/engine.py:375`), en de `AuditMiddleware` schrijft van elke beslissing een rij.
- `_require_flow_access` wordt aangeroepen door:
  - `modal_wizard_init` (GET, regel 664): de modal openen
  - `modal_wizard_load_step` (GET, regel 879): een stap laden
  - `modal_wizard_submit_step` (POST, regel 905): een stap versturen
  - `modal_wizard_skip` (POST, regel 1194)
  - `modal_wizard_confirm` (POST, regel 1210)
  - en nog een keer in `_modal_do_submit` (regel 1409), dat submit_step, skip en confirm aanroepen (regels 1186, 1202 en 1218) als de stroom klaar is
- Voor restore weegt `_handle_backup_restore_submit` daarna nog een derde keer `restore.run` (regel 1677, `authorize`). Bij backup staat op die plek terecht `require_action` (regel 1229).
- Eén restore is dus: init + stap(pen) laden + stap(pen) versturen + 3× in de confirm-request. Dat zijn de acht rijen.
- `tests/test_een_beslissing_per_handeling.py` vangt dit niet. Die test kijkt alleen naar een `authorize` in de BODY van de handler met de action van de ROUTE zelf. De wizardroutes dragen `project.read` of `wizard.use`, en de aanroep zit in een helper.
- `require_action` (`opi/web/authz.py:170`) bestaat al voor precies dit geval: hij weigert met een 403 zonder een beslissing vast te leggen. De HTMX-herrenderroutes van de back-up- en herstelmodal gebruiken hem al (`features/audit.md`, "Het herrenderen van een formulier is geen handeling").

## Wat er moet komen

### 1. Navigeren door de wizard is geen handeling

- Het openen, laden, versturen of overslaan van een stap die de stroom NIET afrondt, weegt de flow-action met `require_action`. De gebruiker zonder recht krijgt nog steeds een 403, maar er komt geen rij.
- Splits `_require_flow_access` in twee functies (voorstel):
  - `_check_flow_access`: `require_action`, voor de navigatie
  - `_authorize_flow`: `authorize`, alleen in `_modal_do_submit`, de plek waar de stroom echt iets doet

  Controleer dat de navigatieroutes de teruggegeven `(project, user_email)` nog krijgen. `require_action` geeft niets terug, dus haal die dan via de bestaande weg (`authorized_project(request)` of wat de route nu al gebruikt). Zeg in de PR welke.
- `_modal_do_submit` is de ENIGE `authorize` met de flow-action per afgeronde stroom.
- Regel 1677 (`trigger_restore`): dezelfde vraag als in `_modal_do_submit`, dus `require_action`, zoals bij backup op regel 1229. Pas het commentaar erboven aan.

### 2. De guard: één beslissing per (action, resource, uitkomst) per verzoek

Een vangnet in `record_decision` (`opi/core/authz/engine.py:375`): is exact dezelfde beslissing (dezelfde action, dezelfde resource, dezelfde uitkomst) in dit verzoek al vastgelegd, dan wordt hij niet nog een keer aan `authz_decisions` toegevoegd. De vier losse attributen (`authz_action` en de rest) blijven gewoon de laatste beslissing dragen, want `@requires_step_up` leest die.

Comply-or-explain: dit is een tweede plek naast de test, en dat is bewust. De test kan niet in helpers kijken zonder een call graph te bouwen, en een helper is precies waar het nu misging. Het vangnet laat de twee rijen van `cloneFrom` staan: dat zijn twee VERSCHILLENDE resources (doel en bron), en dat moet een test vastleggen.

### 3. Tests

- Een routetest die een hele restore-wizard doorloopt (init, stap laden, stap versturen, confirm) met een gemockte taakstart. Daarna staat er precies ÉÉN rij `restore.run` in de audit, met het `task_id` erbij. Gebruik de bestaande fixtures uit `tests/test_audit_hook.py` of de wizardtests voor de setup.
- Dezelfde test voor een gewone bewerkingsstroom (bijvoorbeeld een veld via een modal wijzigen): één rij `project.edit`, met de commit-SHA.
- Een gebruiker zonder het recht krijgt bij het openen van de modal nog steeds een 403, en in de audit staat dan ook een rij. Een geweigerde actie levert altijd een rij op (`features/audit.md`). Toets wat de huidige code doet: als `require_action` bij een weigering geen rij schrijft, is dat een gat. Laat `require_action` dan bij een WEIGERING alsnog een beslissing vastleggen en zeg dat in de PR. Een geweigerde poging hoort in de audit, een toegestane navigatie niet.
- `record_decision`: twee keer dezelfde beslissing levert één element in `authz_decisions` op. Doel en bron van `cloneFrom` leveren er twee op.

Draai gericht met `--timeout`, bijvoorbeeld `uv run pytest tests/test_audit_hook.py tests/test_een_beslissing_per_handeling.py <nieuwe testfile> -x -q --timeout 120`. Geef geen eigen `-m` mee.

### 4. Documentatie

In `features/audit.md`, bij "Hoe de rij ontstaat": de modal-wizard weegt de flow-action bij navigatie met `require_action` en legt de beslissing alleen vast bij het afronden, en `record_decision` ontdubbelt identieke beslissingen binnen één verzoek.

## Wat wordt schrijfbaar, welke guard

Niets wordt schrijfbaar. De guard op elke wizardroute blijft staan: `require_action` weigert met dezelfde 403 als `authorize`, alleen zonder rij bij een toegestane actie. De mutatie zelf (`_modal_do_submit`) houdt `authorize`, inclusief de TOCTOU-hercontrole. Controleer dat `@requires_step_up` op de wizardroutes nog werkt. Die leest `request.state.authz_*`, en dat wordt door `require_action` (via `may`) mogelijk niet gezet. Zoek uit welke wizardroutes step-up dragen en toets ze.

## Afspraken

- Code in het Engels: namen, testnamen, commentaar en docstrings. Zet bestaand Nederlands commentaar niet om.
- Docs in het Nederlands.
- Geen em dashes.
- Daarna `uv run ruff check . --fix`, `uv run ruff format .` en `uv run pyright` in `operations-manager/python`.

## Buiten scope

- De dubbele rijen die al in de database staan. De sandbox mag zo blijven. Staan ze in productie, dan verdwijnen ze met de bewaarlus (na een jaar).
- Zoeken en bladeren in de audit: dat is een eigen taak (`plans/audit-zoeken-en-bladeren.md`).
