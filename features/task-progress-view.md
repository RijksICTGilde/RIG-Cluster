# Eén voortgangsweergave, server-gerenderd

**Status**: Implemented
**Date**: 2026-08-06
**Related**: [Laat een taak zien wat hij doet](./task-steps.md), [Async Task System](./async-task-system.md)

## Wat het is

Elke plek waar een gebruiker op een taak wacht -- de modal na een bevestigde actie, en de
volledige pagina na het aanmaken van een project -- toont dezelfde weergave, gerenderd
door dezelfde server-side partial:

```
opi/templates/partials/task_progress_fragment.html.j2
```

De pagina bouwde die weergave voorheen zelf in JavaScript uit een JSON-endpoint. Dat gaf
twee implementaties van hetzelfde scherm: de stappen die een taak zet (zie
[task-steps](./task-steps.md)) verschenen alleen in de modal, en de datums werden in de
browser opgemaakt in plaats van via het `dutch_date`-filter. Die tweede implementatie is
er niet meer.

## Hoe het werkt

Het fragment vervangt zichzelf elke twee seconden via htmx en stopt met pollen zodra de
taak klaar of mislukt is. Er zijn vier ingangen die hetzelfde fragment vullen:

| Route | Autorisatie | Gebruikt door |
|---|---|---|
| `GET /projects/{project}/task-progress/{task_id}` | `_require_task_of_project` | de modals van de projectdetailpagina |
| `GET /projects/{project}/modal-wizard/progress/{task_id}` | `_require_task_of_project` | de modal-wizard voor projectbewerking |
| `GET /projects/progress/{task_id}/fragment` | `_require_task_access` | de volledige voortgangspagina |
| `GET /projects/progress/{task_id}` | `_require_task_access` | de pagina zelf: het omhulsel, met de eerste weergave er al in |

De pagina is niet meer dan dat omhulsel (kop, kaart, "Terug naar Dashboard"). Ze bevat
geen JavaScript en maakt geen datums op.

De twee routes met een project in hun pad gebruiken `_require_task_of_project`: de taak
moet van dat project zijn, en daarna geldt `_require_task_access`. Een taak van een ander
project geeft 404 en geen 403, hetzelfde antwoord als een taak die niet bestaat.

De twee routes zonder project in hun pad lezen het project uit de taak zelf en gebruiken
alleen `_require_task_access`.

`_require_task_access` (`opi/web/router.py`): je bent geautoriseerd voor het project, of je
bent degene die de taak gestart is. Dat tweede houdt een verwijdering en een aanmaak
volgbaar -- het project staat dan niet (meer) in de store.

## Wat het fragment toont

- een voortgangsbalk, met het percentage naast de huidige stap;
- de stappen en substappen met een vinkje, kruis, pijl of klok;
- de duur van een stap die klaar is (zie [rollout-metingen](./rollout-metingen.md));
- de foutregel van een stap of substap die mislukte;
- bij mislukken: de uitleg per component uit `partials/_component_failures.html.j2`;
- een afsluitknop, als de aanroeper er een meegeeft.

## Context die het fragment verwacht

Verplicht: `task_id`, `progress_url`, `progress`, `current_step`, `status`.
Optioneel: `tasks`, `error`, `component_failures`, `project_name`, `container_id`,
`success_message`, `on_complete` (JavaScript voor de afsluitknop) en `on_complete_label`
(het label van die knop; standaard "Ok" bij slagen en "Sluiten" bij mislukken). De
voortgangspagina gebruikt dat label voor "Naar projectdetails".

## Eén regel om te onthouden

**Render het fragment één keer.** Haal het resultaat niet nog eens door het
`process_components`-filter. De componenttags zijn al vervangen toen de template werd
gecompileerd; een tweede ronde parseert de gerenderde HTML opnieuw als Jinja, en
autoescape ontsnapt `< > & " '` maar niet `{{`. Een stapnaam met `{{ ... }}` erin --
en stapnamen bevatten deployment- en componentnamen uit het projectbestand -- wordt dan
uitgevoerd in plaats van getoond. Zie `render_progress_fragment()` in
`opi/web/task_progress.py`; de test staat in `tests/test_progress_fragment_injection.py`.

## Wat de huidige regel betekent

De regel is de laatst GESTARTE stap, niet de laatst voltooide, dus blijft die regel tot de
volgende stap begint. Elke fase noemt zich zelf, in deze orde: diensten, "Manifesten uit Git
ophalen" per repository, "Manifesten genereren" per deployment, "Projectmanifesten
genereren", "Manifesten naar Git pushen" per repository, "Projectbestand vastleggen",
"ArgoCD-applicaties aanmaken". Faalt een fase, dan staat de foutregel bij die stap.

Het ophalen en de push zijn eigen stappen omdat het netwerk ervoor nodig is. Ze starten
binnen de write, via `on_prepare` en `on_push` van `GitRepoWorkspace.mutate_and_push`. Bij
een botsing starten ze opnieuw. De push-stap van de poging die botste staat dan als
"Manifesten naar Git pushen: botsing, opnieuw" afgerond, niet als mislukt: een uitrol die bij
de volgende poging landt, toont geen rode stap.

Een push, fetch of ls-remote zonder antwoord breekt af na 60s
(`GIT_PUSH_TIMEOUT_SECONDS`) i.p.v. curl's 300s. Dat geldt voor beide git-paden: de
deployments- en argo-repo via `GitRepo`, en de projects-repo via `GitConnector`, waar de
push en de fetch van de rebase dezelfde grens hebben. De foutregel noemt server en repository
zonder credentials, zegt "kreeg geen antwoord" (niet "niet gepusht"; het kan toch geland
zijn), en volgende ronde leest de remote opnieuw.
