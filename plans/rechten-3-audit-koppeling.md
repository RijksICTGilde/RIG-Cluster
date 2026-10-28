# Rechten, taak 3: de audit aan het rechtensysteem koppelen

Opdracht: laat elke schrijfactie en elke weigering een rij in de audit opleveren, zonder code per route, en toon de audit per project. Dit verandert geen gedrag: wat mag en niet mag blijft gelijk.

**Ship dit pas als taak 1 (`plans/rechten-1-rechtensysteem.md`) en taak 2 (`plans/rechten-2-audit.md`) allebei op main staan.** Lees `plans/rechten-op-paden-tad-en-wies.md`, paragraaf 6 en 8.

## Waarom

Taak 1 laat bij elke beslissing de action, de resource, de beslissing en de step-up op `request.state` achter (ontwerp 8.3). Taak 2 levert `record_action` (ontwerp 8.2) en `list_actions`. Deze taak verbindt die twee, en voegt de twee dingen toe die alleen na een verzoek bekend zijn: de statuscode en de commit van het projectbestand.

## Wat er moet gebeuren

### 1. De commit-SHA verzamelen

`ProjectStore.save` en de andere muterende methoden in `opi/services/project_store.py` geven een `MutationResult` met `ref` terug, de commit. Leg elke `ref` van een mutatie met `committed=True` vast in een contextvar per verzoek (zoals de actor in `opi/core/actor.py` een contextvar is). Een verzoek kan meer dan één commit maken; het is een lijst.

*Bewijs:* een test die binnen één verzoek twee keer opslaat en twee SHA's terugziet; een idempotente opslag (`committed=False`) voegt niets toe; een ander verzoek ziet ze niet.

### 2. De hook

Eén middleware die na het antwoord `request.state.authz_action` leest. Is die er niet, dan gebeurt er niets. Is hij er wel, dan schrijft de middleware een rij als de beslissing een weigering is, of als de action geen leesactie is (de markering `read` uit het register van taak 1). De argumenten van `record_action`:

| argument | bron |
|---|---|
| `actor` | de actor van het verzoek |
| `action` | `str(request.state.authz_action)` |
| `resource_path`, `project`, `deployment`, `environment` | `request.state.authz_resource` |
| `allowed`, `cause`, `grant` | `request.state.authz_decision` |
| `step_up` | `request.state.authz_step_up` |
| `status_code` | het antwoord |
| `commit_shas` | de contextvar uit stap 1 |
| `task_id` | de taak die het verzoek startte, als die er is; zoek waar `async_task_service` een taak aanmaakt en leg de id op dezelfde manier vast als de SHA |
| `origin` | `opi/utils/client_ip.py`, `client_ip(request)` |
| `fields` | de velden die de route in `velden=` van `@requires_step_up` noemt, met hun ingediende waarde; geen andere |

Schrijf de rij zonder het antwoord op te houden: na het versturen, als achtergrondtaak, met een eigen databasesessie.

Waarden die niet in het projectbestand staan (welk image, welke back-up): kijk welke schrijfroutes dat hebben. Breid voor die routes `velden=` uit, of voeg naast `@action` een `audit_fields=(...)` toe als er geen step-up op zit. Noem in de PR welke routes dat zijn en waarom het bestaande niet volstond.

*Bewijs:*

- een test die door **elke** route uit de routetabel loopt waarvan de action geen leesactie is, hem één keer toegestaan en één keer geweigerd aanroept, en per aanroep precies één rij ziet met de juiste actor, action, resource en uitkomst;
- een leesroute die toegestaan wordt levert geen rij; een leesroute die geweigerd wordt wel;
- een opslag in een projectbestand levert een rij met de SHA van die commit, en `git show` van die SHA in de test-repo bevat de wijziging;
- een gefaalde `record_action` laat het antwoord ongemoeid.

### 3. Het audit-tabblad

Een tabblad op de projectpagina dat `list_actions` toont: tijd, wie, wat, waarop, uitkomst, en een link naar de commit als die er is. Achter `Action.AUDIT_READ`, met een regel in het register die het aan `admin` geeft. Platformbeheerders zien het op elk project. LOTC-componenten, net als de andere tabbladen in `opi/templates_lotc/bg/project-tabs.html.j2`.

*Bewijs:* een admin ziet het tabblad en de rijen van zijn project; een developer ziet het tabblad niet en krijgt 403 op de route; de matrixpagina toont de nieuwe regel.

### 4. Documentatie

Werk `features/audit.md` uit taak 2 bij: de koppeling, de velden, en het tabblad.

## Wat er buiten valt

- Wat er mag: de regels uit taak 1 veranderen niet, behalve de nieuwe regel voor `audit.read`.
- De tabel en de bewaarlus: taak 2.

## Wat niet mag

- Geen OPI uitrollen.

## Verifieerbaar

1. `uv run ruff check . --fix && uv run ruff format . && uv run pyright` schoon in `operations-manager/python`.
2. De routetabeltest uit stap 2: elke schrijfroute, toegestaan en geweigerd, één rij.
3. De bestaande suite groen, inclusief de matrixtest uit taak 1.
