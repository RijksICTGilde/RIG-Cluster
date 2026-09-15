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
| `create_project` | ja — een project zonder deployments heeft niets uit te rollen; `POST /api/v2/projects` zet de vlag zelf en biedt hem niet aan |
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
  `{project, count, since, task_types, rollout_in_progress}`. Dit is wat een CLI of script kan pollen.

### Hoe de drift gemeten wordt

Twee bronnen, elk voor wat ze echt weten.

**Wat er geschreven is** komt uit de taken: een afgeronde taak waarvan de payload
`rollout: false` was, heeft geschreven en bewust niet verwerkt. Zijn scope staat in
`affects_deployments` (NULL is projectbreed).

**Wat er gereconcilieerd is** legt de verwerking zelf vast, in de tabel
`project_reconciliation` (RC-188). `process_project` schrijft na geslaagde
manifestgeneratie en `create_argocd_resources`:

- een rij per verwerkte deployment van dit cluster (na het scope-filter);
- de projectbrede rij (`deployment_name` NULL) als de run ongescopet was;
- geen rij voor een deployment waarvan de manifestgeneratie een fout vastlegde, en dan
  ook geen projectbrede rij, want die zou die deployment meteen mee afstrepen;
- bij een project zonder deployments op dit cluster alleen de projectbrede rij: er viel
  niets te doen, en anders blijft een uitgestelde wijziging daaraan eeuwig wachten.

Het taaktype doet er niet toe: ook `update_component`, `add_service`, de V1-route en de
nachtelijke resource-tuner verwerken het hele project.

De leesregel, voor een wachtende taak `T` met scope `S`:

```
projectbreed_at  = rij (project, NULL)   of -oneindig
deployment_at(d) = rij (project, d)      of -oneindig

S is NULL  -> T wacht als projectbreed_at < T.completed_at
S concreet -> T wacht als er een d in S is met max(projectbreed_at, deployment_at(d)) < T.completed_at
```

Een deployment op een ander cluster krijgt nooit een eigen rij; een uitgestelde
wijziging daaraan wordt afgestreept door de eerstvolgende volledige verwerking van dit
cluster.

Het opruimen van oude taken leest dezelfde regel: een uitgestelde uitrol die nog niet is
gereconcilieerd wordt **niet** verwijderd, ongeacht leeftijd. Anders zou de melding na
een week stil verdwijnen, precies de stille drift die dit moet voorkomen.

`rollout_in_progress` gaat over OPEN taken, waarvoor nog niets is vastgelegd: waar is een
open taak uit `PROCESSING_TASK_TYPES`, zonder `rollout: false`, waarvan de scope alles
dekt wat wacht (`covers()`). Omdat `scope_of()` een paar projectbrede handlers gescopet
noemt (`configure_service`, `configure_service_values`, `manage_database_schemas`), meldt
dit soms geen lopende uitrol terwijl die er wel is: de veilige kant.

### De grens is het LEESMOMENT van de verwerking, niet het einde

Een verwerking leest het projectbestand **één keer, aan het begin van zijn eigen run**,
en werkt de rest van de looptijd met die momentopname. Alles wat daarna wordt opgeslagen
zit er niet in, hoe lang de run daarna ook nog draait. Dat is meestal minuten, want de
ArgoCD-wacht domineert.

Daarom slaat de rij het moment op waarop de run het projectbestand las, en niet wanneer
hij klaar was. Met het eindtijdstip werd een uitgestelde wijziging die tijdens een
lopende refresh werd opgeslagen weggestreept door een refresh die hem nooit gelezen had
(RC-82). Het moment wordt op de databaseklok berekend, dezelfde klok als `completed_at`
van de taken: `now()` min hoe lang geleden de run las. Lopen twee runs over elkaar, dan
wint de latere lezing (`GREATEST`).

Bij de invoering (migratie 006) is per project de uitkomst van de oude meting als
projectbrede rij weggeschreven, zodat de teller op het omschakelmoment hetzelfde leest en
pas vanaf de eerstvolgende verwerking beter wordt.

Welke wijzigingen binnen dat venster kunnen vallen volgt uit de wachtrij. Taken zonder
deployment in hun sleutel (`add_component`, `update_component`, `add_service`,
`configure_service`) worden achter een projectbrede refresh geserialiseerd en landen er
dus nooit in. Taken mét een deployment (`update_image`, `upsert_deployment`) lopen wél
gelijktijdig, en dat waren precies de gevallen die verdwenen.

Gemeten in `tests/test_refresh_merge_window.py`, dat ook vastlegt dat een tweede refresh
tijdens een lopende dezelfde taak teruggeeft (ontdubbeling op identiek lichaam) en dat
er in die lopende taak niets opnieuw wordt gelezen.

## Wat het oplevert

Gemeten bij RC-117 op een sandboxcluster, tien keer `add_component` op hetzelfde project:

| | Tijd |
|---|---|
| tien keer met uitrol (de standaard) | ~735s |
| tien keer `rollout=false` plus een `:refresh` | **67s** |

Elf keer sneller, en het is te verklaren: van een uitrollende actie gaat 90% naar het
wachten op ArgoCD en 8% naar het genereren van de manifesten. Het committen en pushen
van het projectbestand - het deel dat je per handeling betaalt en dat blijft staan - is
1,6%. Daarom is dit de vlag om te gebruiken bij een reeks handelingen, en niet iets aan
git. De hele meting staat in `docs/rc117-veel-acties-meting.md`.

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
