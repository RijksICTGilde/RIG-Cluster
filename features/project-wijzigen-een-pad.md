# Een project wijzigen: één pad

Een projectbestand wijzigen had tien ingangen: de bewerkwizard, de drie wegen van een
bewerkmodal (stap, skip, confirm), de goedkeuringsroute, het env-vars-venster, de
v1-upsert async, de v1-upsert met `?sync=true`, de v2-upsert en de git monitor. Ze hadden
drie verschillende merges, drie validatiesets, een eigen uitrolscope per ingang en elk een
eigen manier om een fout om te zetten. Sinds RC-483 lopen ze door dezelfde use case.

## Waar het pad loopt

```
ingang (route of task handler)
   |  vertaalt invoer naar een changeobject, en uitkomst of exception naar zijn antwoord
   v
opi/manager/project/
   update.py            update_project(ProjectChange)      -> ProjectUpdateResult
   deployment_upsert.py upsert_deployment(DeploymentUpsert) -> DeploymentUpsertResult
   errors.py            ProjectChangeError met zijn error_type
   edit_authority.py    EditAuthority, het protocol waartegen de weging loopt
   |
   v
opi/manager/project/deployment_mutations.py   de schrijfkern van een upsert
opi/services/project_store.py                 de enige gevalideerde schrijfweg
```

Welke ingang welke use case gebruikt:

| ingang | use case |
|---|---|
| bewerkwizard `POST /forms/wizard/{flow}/submit` (bewerkmodus) | `update_project` |
| bewerkmodal `POST /projects/{p}/modal-wizard/{flow}/step/{sectie}` | `update_project` |
| bewerkmodal `POST .../skip` en `POST .../confirm` | `update_project` |
| goedkeuringsroute `POST /admin/approvals/{p}/modal-wizard/{flow}/step/{sectie}` | `record_approval_decision` |
| v1 `POST /api/projects/{p}/:upsert-deployment` (met en zonder `?sync=true`) | `upsert_deployment`, via de taakrij |
| v2 `POST /api/v2/projects/{p}/:upsert-deployment` | `upsert_deployment`, via de taakrij |
| env-vars-venster | `set_component_values` (zie `env-vars-en-service-values-een-pad.md`) |

## Wat `update_project` doet

1. Een verse read uit git. Diezelfde read is de compare-and-swap-basis van de opslag, dus
   een wijziging die tussendoor landde wordt door de store samengevoegd.
2. De `apply` van de ingang: zijn eigen merge, op die verse staat.
3. De guards uit `ProjectChange.guards`, een `frozenset[EditGuard]`:

   | guard | wat hij doet |
   |---|---|
   | `PLATFORM_FIELDS` | zet elk serviceconfigveld terug dat het platform zelf schrijft |
   | `HIDDEN_MEMBERS` | zet de service accounts en tokenhashes terug die het teamformulier niet draagt |
   | `APPROVAL_REQUESTS` | legt een aanvraag vast voor alles wat het project vraagt en niet heeft |
   | `MEMBER_ROLE` | weegt `member.manage` als de ledenlijst wijzigt |
   | `MARKING` | weegt `environment.set` als een productiemarkering wijzigt |
   | `CLONE_SOURCE` | weegt `deployment.clone-from` op de bron van elke clone-from die deze opslag zet |
   | `STEP_UP` | eist een verse tweede factor voor een wijziging die productie raakt |

4. De opslag via `ProjectStore.save`, die als enige valideert en commit.
5. De uitrolscope, uit het verschil tussen voor en na.

De weging loopt via `EditAuthority`, een `Protocol` met `require`, `step_up_is_fresh` en
`actor`. De enige implementatie is `RequestAuthority` in
`opi/http/auth/request_authority.py`. Zo weegt de use case een action zonder iets van
`Request` of van de entry layer te weten.

## De uitrolscope

`deployments_touched(before, after)` in `opi/services/project_utils.py` is de enige regel.
Geen ingang weet wat zijn merge raakte, dus de scope komt uit het verschil. Hij leunt
bewust naar breed, want een scope die te smal is laat het cluster achter het
projectbestand terwijl een te brede alleen tijd kost:

- een verschil buiten `deployments` en `components` is projectbreed (`None`);
- een VERWIJDERDE deployment is projectbreed, want een gescopete ronde haalt zijn
  manifesten er niet uit;
- anders: de deployments met een gewijzigd eigen blok, plus de deployments die een
  gewijzigd component gebruiken.

Een lege lijst betekent "geen enkele deployment hoeft iets", en dan start er geen ronde.
Wie met deze lijst een ronde versmalt, legt dezelfde lijst vast als de taakscope
(`opi.services.tasks.task_supersede.scope_of`).

De uitrol zelf is de taak `rollout_project`: hij wist de OOM-tunestaat van de deployments
die hij verwerkt, verwerkt ze, en bewaakt ze daarna. Vroeger leenden deze ingangen de
`create_project`-taak met `project_file_already_saved=True`, die het verkeerde zei in de
takenlijst, de ronde niet kon versmallen en de volledige ciphertext van het project in de
taakrij zette.

## Fouten

De use cases raisen typed exceptions uit `opi/manager/project/errors.py`. Elke exception
draagt het `error_type` dat de API en de taakuitslag altijd al gebruikten, zodat een ingang
geen eigen tabel nodig heeft:

| exception | wat de ingang ermee doet |
|---|---|
| `StepUpRequiredError` | de wizard geeft 403, de modal de step-up-dialoog |
| `ProjectChangeError` | de API leest `error_type` en kiest zijn statuscode |
| `ProjectSchemaError`, `ProjectIntegrityError`, `ConflictError` (uit de store) | her-render met de melding |

## Synchroon antwoorden via de taakrij

`?sync=true` op de v1-upsert draait het werk niet meer naast de rij: hij zet dezelfde
`upsert_deployment`-taak in de rij en wacht die uit met `wait_for_task_answer`. Daarmee
gelden de taakordening, de dedup, de drain-guard en de federatie-routering ook voor hem,
en blijft de antwoordvorm gelijk.

Eén geval is nieuw, want door de rij is het pas mogelijk: een taak die wordt overgenomen
door een nieuwere, bredere taak (`superseded`, zie `task-supersede.md`). Het projectbestand
is dan gecommit en alleen de uitrol is verhuisd, dus de route antwoordt 202 met de
`task_id`, de `poll_url` en `superseded_by`, net als de asynchrone tak. Niet de succesvorm:
een overname draagt geen `deployment` en geen `urls`.

## Wat nog niet dicht is

De bewerkwizard schrijft alleen de paden die zijn flow verklaart, maar de bewerkflow
verklaart `deployments`, `components`, `services` en `users` als hele lijsten. Een
gelijktijdige wijziging in zo'n lijst gaat daardoor nog verloren: de sessie draagt de lijst
zoals hij was toen de wizard openging. Dichtmaken vraagt dat de opslag vergelijkt met de
versie waaruit de SESSIE gezaaid is in plaats van met een verse read. Vastgelegd in
`tests/test_wizard_edit_write_paths.py`.
