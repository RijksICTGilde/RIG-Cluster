# Real-life E2E suite (gelijktijdige wijzigingen op één projectfile, x5 projecten)

Een aparte, langdurige sandbox-suite die nabootst wat echte gebruikers op een drukke dag doen:
vijf projecten die door elkaar heen worden aangepast via de web-UI en de REST API. Na **elke**
wijziging wordt de projectfile in de Forgejo-repo `zad-projects` gecontroleerd - de HTTP-respons
alleen wordt nooit vertrouwd.

Bestand: `operations-manager/python/tests/e2e/test_sandbox_reallife.py`

## Waar het echt om gaat: gelijktijdigheid op dezelfde file

Twee gebruikers die verschillende projecten bewerken is niet interessant: dat zijn verschillende
bestanden, die kunnen niet botsen. Het risico zit in twee wijzigingen die op **dezelfde**
projectfile racen. Beide lezen dezelfde YAML, beide passen hem aan, en een naïeve
read-modify-write publiceert de tweede over de eerste heen - waarmee de wijziging die er tussendoor
landde stilletjes verdwijnt (een lost update).

Elke ronde vuurt daarom meerdere mutaties af op hetzelfde project - via de API zonder te wachten,
en via de browser terwijl die taken nog lopen - en controleert daarna of ze er **allemaal samen**
in staan. Dat gebeurt voor vijf projecten, zodat een race die zich maar af en toe voordoet vijf
kansen per ronde krijgt om zichtbaar te worden.

## Waarom apart

Deze suite draait **niet** mee in de standaard testset. Hij duurt lang en heeft een draaiende
sandbox nodig met jouw build erop. Daarom een eigen marker, `reallife`, en een eigen task. De
reguliere sandbox-run sluit hem expliciet uit met `-m "e2e and sandbox and not reallife"`.

## Draaien

```bash
sandbox-deploy                     # zet JOUW build op de cluster en verifieert /version
task test-e2e-sandbox-reallife     # draait deze suite
sandbox-release                    # geef de sandbox weer vrij
```

Zonder `E2E_BASE_URL` skipt alles automatisch (de `sandbox_url`-fixture).

## De rondes

| Test | Wat er tegelijk gebeurt op één file | Wat er in git gecontroleerd wordt |
|---|---|---|
| `test_create_five_projects` | 5x de wizard | display-name, teamlid, component `web`, deployment-referentie |
| `test_concurrent_component_adds_on_same_file` | `alpha` en `beta` worden tegelijk toegevoegd | beide aanwezig, `web` niet gesneuveld, alle drie in de deployment, env-vars AGE-versleuteld |
| `test_concurrent_patches_on_same_file` | drie patches, elk op een ander component | drie verschillende geheugenlimieten staan er tegelijk in |
| `test_ui_edits_while_api_task_runs_on_same_file` | API voegt `gamma` toe terwijl de browser beschrijving én teamlid wijzigt | alle drie de wijzigingen, plus eerdere rondes nog intact |
| `test_ui_env_vars_while_api_patches_same_file` | UI zet env-vars op `web` terwijl de API het image wijzigt | versleutelde env-vars én het nieuwe image; plaintext lekt niet; waarde weer leesbaar in de UI |
| `test_ui_removal_while_api_patches_same_file` | UI verwijdert `beta` terwijl een API-patch `alpha` herschrijft | `beta` weg uit componenten én deployment, patch op `alpha` overleefde |
| `test_add_deployment_rolls_out_whole_project` | een tweede deployment erbij (rolt het hele project opnieuw uit) | beide deployments aanwezig, elk met hun componenten |
| `test_final_state_of_all_projects` | geen mutatie, alleen controle | de volledige eindtoestand van alle vijf files |

Die laatste is de belangrijkste. Elke ronde controleert alleen zijn eigen wijziging; deze telt alle
rondes bij elkaar op per project. Een lost update uit ronde twee wordt pas hier zichtbaar.

## Hoe de gelijktijdigheid wordt afgedwongen

`sandbox_api.start_task()` vuurt een async API-call af en geeft direct het task-id terug **zonder te
wachten**. Zo staan er meerdere mutaties open terwijl de browser hetzelfde project bewerkt.

Het is bewust "semi"-gelijktijdig: de UI-handelingen zijn serieel (één browser), de API-taken
overlappen met elkaar én met die UI-handelingen.

## Waarom de git-controle niet op de taak wacht

De volgorde in een taak is: projectfile committen en pushen, **daarna** pas manifests genereren en
ArgoCD laten syncen. De projectfile - waar deze suite over gaat - staat dus al vast lang voordat de
taak klaar is.

Daarom controleert elke ronde eerst git (`_assert_in_git`, die pollt tot de commit er is) en pas
daarna de taken (`_settle_tasks`). Die laatste laat een test alleen falen bij een **expliciete**
taakfout; een taak die nog draait wordt gelogd maar niet als fout gerekend, want dat zegt iets over
de snelheid van de cluster en niets over het projectfile-mechanisme.

## Keuze van het test-image

`nginxinc/nginx-unprivileged:stable-alpine`. De gegenereerde deployment heeft **drie** probes
(startup, liveness en readiness) die allemaal op de inbound-poort prikken, dus een container die
alleen draait maar niets serveert (busybox met `sleep`) wordt nooit Ready - waarna het opruimen
blijft hangen op ArgoCD. Dit image is klein, draait non-root en luistert op 8080.

## Volgorde en gedeelde staat

De tests zijn **geordend** en delen een module-scoped fixture (`reallife_projects`). Draai het
bestand als geheel; een losse `-k`-selectie werkt niet, want latere tests bouwen voort op de
toestand die eerdere achterlaten. De fixture ruimt aan het eind alle vijf projecten op via de API,
ook als een test faalde.

## Herbruikbare onderdelen

- `tests/e2e/helpers/lifecycle.py` - `create_project_via_wizard()` doorloopt de wizard en levert de
  technische projectnaam, de API-key en de eerste deployment op. Die naam heeft een willekeurig
  achtervoegsel en wordt gevonden door de repo-listing voor/na te vergelijken.
- `ForgejoClient.wait_for_condition()` - pollt de geparste YAML tot een predicaat klopt. Bij falen
  komt de volledige file in de foutmelding, want bij gelijktijdig schrijven is juist wát er wél in
  staat de diagnose.
- `sandbox_api.start_task()` / `wait_for_task()` / `task_outcome()` - async API-calls afvuren en hun
  afloop ophalen; `task_outcome()` onderscheidt "mislukt" van "draait nog".
- `EditModalHelper.sequence_add()` / `.sequence_remove()` - bedienen de repeat-velden (teamleden,
  componenten) via de eigen `sequenceAdd`/`sequenceRemove`-handlers van de pagina.

## Aandachtspunten

- **Draai eerst `sandbox-deploy`.** Zonder dat test je de oude image die toevallig op de cluster
  staat.
- De suite maakt echte projecten aan met echte namespaces, databases en ArgoCD-applicaties.
- Componentnamen mogen alleen kleine letters en cijfers bevatten - vandaar `web`, `alpha`, `beta`,
  `gamma`.

## Wat de eerste volledige run opleverde

Draaien tegen build `fa13306` (5 projecten, 8 rondes):

- **De project-store hield stand.** Alle commits landden: bij vijf gelijktijdige component-adds
  kwamen `alpha` en `beta` allebei correct in dezelfde file, en ook de latere patches, de
  identity-wijziging en de extra deployment zijn gecommit. Geen lost update waargenomen.
- **Een echte bug gevonden**, deterministisch in 5 van de 5 projecten: `update_component` schrijft
  `memory_limit` naar `resources.memory`, terwijl de manifest-resolver
  (`project_file_handler._resolve_resources`) uitsluitend `resources.requests` en `resources.limits`
  leest. De opgegeven limiet wordt dus genegeerd en de container krijgt de default van 512Mi.
  Het JSON-schema staat `resources.memory` toe als legacy-vorm, dus de commit slaagt gewoon.

## Zie ook

- `features/e2e-sandbox-tests.md` - de fixtures waar deze suite op staat.
- `features/version-endpoint.md` - controleren welke build er draait.
