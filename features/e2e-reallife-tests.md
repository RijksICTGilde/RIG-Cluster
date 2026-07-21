# Real-life E2E suite (5 projecten, semi-gelijktijdig via UI + API)

Een aparte, langdurige sandbox-suite die nabootst wat een groep echte gebruikers op een drukke dag
doet: vijf projecten naast elkaar, door elkaar heen aangepast via de web-UI en de REST API.
Na **elke** wijziging wordt de projectfile in de Forgejo-repo `zad-projects` gecontroleerd - de
HTTP-respons alleen wordt nooit vertrouwd.

Bestand: `operations-manager/python/tests/e2e/test_sandbox_reallife.py`

## Waarom apart

Deze suite draait **niet** mee in de standaard testset. Hij duurt lang (vijf wizard-creaties plus
een reeks projectverwerkingen) en heeft een draaiende sandbox nodig met jouw build erop. Daarom een
eigen marker, `reallife`, en een eigen task. De reguliere sandbox-run (`task test-e2e-sandbox`)
sluit hem expliciet uit met `-m "e2e and sandbox and not reallife"`.

## Draaien

```bash
sandbox-deploy                     # zet JOUW build op de cluster en verifieert /version
task test-e2e-sandbox-reallife     # draait deze suite
sandbox-release                    # geef de sandbox weer vrij
```

Zonder `E2E_BASE_URL` skipt alles automatisch (de `sandbox_url`-fixture).

## Wat het aantoont

De kern is de **projectfile in git als enige waarheid**. Elke test muteert via de echte UI of API en
leest daarna de YAML terug uit Forgejo via `ForgejoClient`.

| Test | Wat er gebeurt | Wat er in git gecontroleerd wordt |
|---|---|---|
| `test_create_five_projects` | 5x de wizard doorlopen | display-name, teamlid, component `web`, deployment-referentie |
| `test_add_components_via_api_concurrently` | 5 component-adds tegelijk afgevuurd, daarna pas afgewacht | component `worker` in alle 5 files, image, en `web` nog intact |
| `test_api_resource_patch_while_ui_edits_description` | API-patch op project 0-2 terwijl de browser 3-4 bewerkt | geheugenlimiet 384Mi resp. nieuwe beschrijving |
| `test_user_env_vars_via_ui` | env-vars zetten via de componenten-modal | AGE-versleuteld in git, plaintext lekt **niet**, en weer leesbaar op de detailpagina |
| `test_api_image_update_while_ui_adds_team_member` | image-update op 3-4 terwijl de UI een teamlid toevoegt aan 2 | image in deployment resp. e-mail in `users` |
| `test_remove_components_via_ui_while_api_patches` | component verwijderen via UI op 0 en 4, met een API-patch op 2 in de lucht | `worker` weg uit componenten én deployment; patch op 2 toch geland |
| `test_final_state_of_all_projects` | geen mutatie, alleen controle | de volledige eindtoestand van alle 5 files |

Die laatste is de belangrijkste: hij telt alle rondes bij elkaar op per project. Bij gelijktijdig
schrijven op een read-modify-write-store kan een eerdere wijziging stilletjes verdwijnen (lost
update); een test die alleen zijn eigen mutatie controleert ziet dat niet, deze wel.

## Hoe de gelijktijdigheid werkt

`sandbox_api.start_task()` vuurt een async API-call af en geeft direct het task-id terug **zonder te
wachten**. Zo staan er meerdere mutaties tegelijk open terwijl de browser ondertussen een ander
project bewerkt. `sandbox_api.wait_for_task()` haalt ze daarna op; `_wait_all()` verzamelt álle
mislukkingen in plaats van te stoppen bij de eerste.

Het is bewust "semi"-gelijktijdig: de UI-handelingen zijn seriëel (één browser), de API-taken
overlappen met elkaar én met die UI-handelingen.

## Volgorde en gedeelde staat

De tests in dit bestand zijn **geordend** en delen een module-scoped fixture (`reallife_projects`).
Draai het bestand als geheel; een losse `-k`-selectie werkt niet, want latere tests bouwen voort op
de toestand die eerdere hebben achtergelaten. De fixture ruimt aan het eind alle vijf projecten op
via de API (force delete), ook als een test faalde.

## Herbruikbare onderdelen

Bij het bouwen zijn een paar dingen uit `test_sandbox_flows.py` gehaald zodat beide suites ze delen:

- `tests/e2e/helpers/lifecycle.py` - `create_project_via_wizard()` doorloopt de wizard en levert het
  technische projectnaam, de API-key en de eerste deployment op. De technische naam heeft een
  willekeurig achtervoegsel en wordt gevonden door de repo-listing voor/na te vergelijken.
- `ForgejoClient.wait_for_condition()` - pollt de geparste YAML tot een predicaat klopt. Commits
  landen asynchroon na een mutatie, dus wachten is nodig; falen levert de volledige file in de
  foutmelding.
- `EditModalHelper.sequence_add()` / `.sequence_remove()` - bedienen de repeat-velden (teamleden,
  componenten) via de eigen `sequenceAdd`/`sequenceRemove`-handlers van de pagina.

## Aandachtspunten

- **Draai eerst `sandbox-deploy`.** Zonder dat test je de oude image die toevallig op de cluster
  staat. `test_version_endpoint` in de reguliere suite controleert dit.
- De suite maakt echte projecten aan met echte namespaces, databases en ArgoCD-applicaties. Op een
  trage cluster kan een ronde minuten duren; de per-test timeout staat daarom op 600s.
- Componentnamen mogen alleen kleine letters en cijfers bevatten - vandaar `web` en `worker`.
- Het gebruikte image (`nginxinc/nginx-unprivileged`) luistert op 8080 en draait non-root, zodat de
  deployments gezond worden en het opruimen niet blijft hangen.

## Zie ook

- `features/e2e-sandbox-tests.md` - de fixtures en het bouwmateriaal waar deze suite op staat.
- `features/version-endpoint.md` - controleren welke build er draait.
