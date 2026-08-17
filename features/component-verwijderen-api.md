# Een component verwijderen via de API

Een component kan via de REST-API weer weg. `DELETE` op hetzelfde pad waar `PATCH` al
zat:

```
DELETE /api/v2/projects/{project}/components/{component}
```

## Waarom dit er niet al was

De machinerie lag er volledig: `TaskType.DELETE_COMPONENT` bestaat,
`handle_delete_component` bestaat en is geregistreerd, en het portaal verwijdert
componenten er al mee. Alleen de route ontbrak, en dat is van buiten niet te zien: het pad
antwoordde alleen op `PATCH`, dus een client kon een component aanmaken en wijzigen maar
nooit weghalen. De zad-cli meldde dat zo:

```
Error: This platform has no way to delete a component, so 'web' was left alone.
  why: The API offers only PATCH on /v2/projects/{project}/components/{component}.
```

Die melding klopt niet meer.

## Wat er gebeurt met een component dat in gebruik is

Een component kan door meerdere deployments gedeployd worden en door andere componenten
als afhankelijkheid genoemd worden (`uses-components`). Dezelfde afweging als bij het
verwijderen van een bijlage (RC-52), en dezelfde uitkomst:

| situatie | antwoord |
|---|---|
| niets verwijst ernaar | 202, de taak verwijdert het |
| iets verwijst ernaar | **409** met `used_by`: elke plek bij naam |
| iets verwijst ernaar, `confirm_in_use=true` | 202; de verwijzingen gaan in dezelfde opslag mee |
| het webadres van een deployment is eromheen gebouwd | **409**, ook mét `confirm_in_use` |
| het component bestaat niet | **404** |

```bash
# weigert, en zegt waar
curl -X DELETE -H "X-API-Key: $KEY" \
  https://zad.example/api/v2/projects/demo/components/web
# 409
# {"detail": {"detail": "Component 'web' is in gebruik door: deployment 'staging'. Set
#             confirm_in_use=true to remove those references along with it.",
#             "used_by": [{"deployment": "staging", "component": null,
#                          "kind": "deployment", "label": "deployment 'staging'"}]}}

# met de bevestiging: het component en de verwijzingen gaan samen weg
curl -X DELETE -H "X-API-Key: $KEY" \
  "https://zad.example/api/v2/projects/demo/components/web?confirm_in_use=true"
# 202 {"task_id": "..."}
```

`confirm_in_use` is genoemd naar wat de aanroeper *verklaart*, niet naar wat het
overrulet: dat hij de lijst uit de 409 gezien heeft. Het opruimen is één opslag, nooit
twee die half kunnen slagen — een verwijzing naar een component dat niet meer bestaat
maakt het projectbestand ongeldig.

### De ene uitzondering

Een component waar het webadres van een deployment omheen gebouwd is (`root-component` of
`expose-component-on-bare-domain`) wordt ook mét bevestiging geweigerd. Die verwijzing
weghalen zonder te beslissen hoe de site dan wél bediend wordt, is geen beslissing die een
verwijderactie mag nemen. Wijzig eerst het webadres daar. Dit is precies de regel die een
publish-on-web-certificaat bij bijlagen krijgt.

## Uitrol

De taak verwerkt het project altijd opnieuw — anders blijft het component in het cluster
draaien terwijl het uit het projectbestand weg is. `rollout=false` wordt daarom met 422
geweigerd in plaats van stilzwijgend genegeerd.

## Resultaat van de taak

Poll `/api/tasks/{task_id}`:

```json
{
  "status": "completed",
  "message": "Component 'web' succesvol verwijderd",
  "project": "demo",
  "component": "web",
  "uncoupled_from": [{"deployment": "staging", "component": null,
                      "kind": "deployment", "label": "deployment 'staging'"}],
  "processing": {"status": "completed"}
}
```

`uncoupled_from` is leeg tenzij `confirm_in_use` nodig was: dan staat er wat er mee
veranderd is.

## In het portaal

Het bevestigingsvenster noemt de deployments en componenten die meeveranderen, en post
daarna mét de bevestiging — de gebruiker heeft de lijst immers gezien. Voor een component
waar een webadres omheen gebouwd is toont het venster de reden en geen knop, zoals het dat
voor een bijlage in gebruik al deed.

## Waar het staat

| | |
|---|---|
| route | `opi/api/v2/router.py` — `delete_component_v2` |
| guard + opruimen | `ProjectManager.delete_component` |
| de wandeling | `component_usage_sites` / `remove_component_references` in `opi/handlers/project_file_handler.py` |
| taak | `handle_delete_component` in `opi/core/task_handlers_components.py` |
| bevestigingsvenster | `opi/web/project_actions.py` |
