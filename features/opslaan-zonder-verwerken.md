# Opslaan zonder verwerken (`rollout=false`)

In de opbouwfase van een project wil je tien dingen achter elkaar toevoegen en daarna
één keer verwerken, niet tien keer een volledige uitrol uitlokken. Elk v2-endpoint dat
normaal het project verwerkt accepteert daarom `rollout=false`.

## Wat het doet

```
PUT /api/v2/projects/{project}/services/keycloak/config/project?rollout=false
  -> de wijziging is gevalideerd, opgeslagen en gecommit in het projectbestand
  -> geen manifestgeneratie, geen provisioning, niets naar het cluster

POST /api/v2/projects/{project}/:refresh
  -> nu wel, in een keer, voor alles wat je hebt opgespaard
```

Standaard blijft verwerken. Wie niets meegeeft krijgt exact het gedrag van daarvoor;
dit is een uitzondering die je aanvraagt, geen instelling die je vergeet.

## Waar het wel en niet mag

| Endpoint / taaktype | `rollout=false` |
|---|---|
| `upsert_deployment`, `add_component`, `update_component`, `add_component_to_deployment`, `add_service`, `configure_service` (alle per-dienst config-routes), `update_image` | ja |
| `refresh_project`, `refresh_deployment` | nee — verwerken is de hele handeling |
| `delete_deployment` | nee — verwijderen haalt clusterbronnen weg; een refresh verwerkt wat het projectbestand declareert en zou de verwijdering nooit alsnog uitvoeren |
| `clone_database`, `clone_bucket` | nee — die werken rechtstreeks op het cluster en schrijven niets in het projectbestand |

Waar het niet mag wordt de vlag **geweigerd** met HTTP 422 en de reden erbij, niet stil
genegeerd. De indeling staat in `opi/core/task_rollout.py` (`DEFERRABLE_TASK_TYPES` en
`NON_DEFERRABLE_REASONS`); een taaktype dat in geen van beide staat wordt door een test
gemeld.

## Wat je terugkrijgt

De taakuitkomst zegt wat er niet gebeurd is, zodat een script het weet zonder de
documentatie te lezen:

```json
{
  "status": "success",
  "processing": {
    "status": "skipped",
    "reason": "rollout_disabled",
    "message": "Change saved to the project file; not rolled out because rollout=false ..."
  }
}
```

`status: "skipped"` bestond al voor "er was niets te doen" (een `clear` die niets vond,
een dienst die al geselecteerd was). De `reason` onderscheidt de twee: alleen een
uitgestelde uitrol draagt `rollout_disabled`.

In de voortgang van de taak verschijnt geen verwerkingsstap; in plaats daarvan staat er
één afgeronde stap **"Uitrol overgeslagen (rollout=false)"**.

## De drift zichtbaar maken

Een projectbestand dat vooruitloopt op het cluster is gevaarlijker dan een trage uitrol,
want een trage uitrol merk je. Daarom:

- **Projectdetailpagina**: boven de tabs een waarschuwing zodra er onverwerkte
  wijzigingen liggen, met het aantal, sinds wanneer de oudste wacht, en de knop
  "Nu verwerken" (dezelfde bevestiging als "Project herverwerken").
- **API**: `GET /api/v2/projects/{project}/pending-rollout` geeft
  `{project, count, since, task_types}`. Dit is wat een CLI of script kan pollen.

### Hoe de drift gemeten wordt

Uit de taken zelf, want die zijn het bewijs van de schrijfactie: een afgeronde taak
waarvan de payload `rollout: false` was, heeft geschreven en bewust niet verwerkt.
Alles daarvóór wordt opgeruimd door een taak die het **hele** project verzoent —
`refresh_project` en `delete_component` (die intern dezelfde refresh draait). Bewust
smal: `refresh_deployment` of een `add_component` mét uitrol raakt maar één deployment
en laat de rest van het bestand vooruitlopen, dus die tellen niet als "uitgerold".

Daarbij hoort één aanpassing aan het opruimen van oude taken: een uitgestelde uitrol die
nog niet is uitgerold wordt **niet** verwijderd, ongeacht leeftijd. Anders zou de
melding na een week stil verdwijnen — precies de stille drift die dit moet voorkomen.

## Voor ontwikkelaars

De vlag reist mee in de taak-payload (`payload["rollout"]`) en wordt op één plek gelezen:
in de handler, op het punt waar die anders `process_project_from_git` zou aanroepen. Bij
`update_image` zit die splitsing een laag dieper, in
`ProjectManager.update_image_and_regenerate(rollout=...)`, omdat die methode schrijven en
verwerken in één aanroep doet: met `rollout=False` stopt hij na de commit, vóór
`process_project()` en de ArgoCD-sync.

Een nieuw endpoint dat verwerkt hoort de vlag door te geven in zijn payload; de handler
hoeft dan alleen `rollout_requested(payload)` te lezen.

## Nog niet gedaan

- **`zad-cli`**: de CLI en zijn spec-kopie in `api/upstream-openapi.json` liggen in een
  andere repository en zijn hier niet bereikbaar. De parameter en het
  `pending-rollout`-endpoint staan wel in `/openapi.json` van een draaiende instantie,
  dus de CLI-kant kan daarop worden bijgewerkt.
- **`post_save_action="save_only"`** (de formulierkant, gebruikt door invite) is nog een
  eigen mechanisme. Dat samenvoegen met deze vlag raakt de wizard-opslagstap, die op dit
  moment door RC-43 en RC-44 wordt verbouwd; het is bewust buiten deze wijziging
  gehouden.
