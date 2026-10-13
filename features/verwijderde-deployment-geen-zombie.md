# Een verwijderde deployment komt niet terug als zombie

Een deployment verwijderen kon een zombie opleveren: de deployment stond niet meer in het
projectbestand, maar zijn ArgoCD Application en manifestmap stonden nog in git, en ArgoCD
hield hem draaiend. Er wees niets meer naar, dus niemand ruimde hem op. In productie
stonden er op 9 oktober 2026 drie, elk met een draaiende pod en een publieke Ingress
(`dd-mco-pr-41`, `pm-5sj-pr139`, `toets-hn7-pr-32`).

Er waren twee oorzaken, beide in `DeleteProjectManager.delete_deployment`.

## Oorzaak A: een gelijktijdige regeneratie zette de Application terug

`delete_deployment` haalt het argo-bestand weg in stap 2 en haalt de deployment pas in
stap 8 uit het projectbestand. Daartussen zit de wacht op ArgoCD: minuten. Draait in dat
venster `ProjectManager.process_project` voor hetzelfde project, dan leest die het
projectbestand nog mét de deployment, en schrijft `create_argocd_resources` de Application
opnieuw. Elke taak heeft een eigen clone van de argo-repo, dus de push zet hem terug.

Gemeten in de argo-applications-repo en de projects-repo:

| | dd-mco/pr-41 (02-09) | toets-hn7/pr-32 (12-06) |
|---|---|---|
| stap 2: argo-bestand weg | 10:08:09 | 09:20:55 |
| regeneratie zet het terug | 10:08:35 | 09:21:51 |
| stap 8: deployment uit projectbestand | 10:08:42 | 09:21:52 |

### Wat er nu gebeurt

`process_project` slaat de deployments over waarvoor een delete-taak open staat:
`task_type == "delete_deployment"`, hetzelfde project, status `pending`, `claimed` of
`running`. De query is `AsyncTaskService.find_open_delete_deployments(project_name)` en
geeft per deployment de task-id, zodat de overslagmelding die kan noemen.

Het filter zit op de plek waar de deployments van de ronde worden bepaald, dus het geldt
voor alles wat de ronde per deployment doet: namespaces, SOPS-secrets, provisioning van
diensten, manifesten, ArgoCD-resources en bootstrap. Een projectbrede ronde
(`targets is None`) wordt daarbij een expliciete opsomming van alle deployments min de
open deletes, want anders regenereert `create_argocd_resources` weer alles uit
`project_data`. Voor de reconciliatierij blijft zo'n ronde projectbreed: de grendel
versmalt het werk, niet wat de aanroeper vroeg.

De ArgoCD-synchronisatiewacht in `process_project_from_git` laat ze ook weg. Er is geen
Application om op te wachten, dus die wacht zou aflopen op de aanmaaktimeout en de taak
laten falen op een deployment die aan het verdwijnen is.

Een delete-taak die faalt is niet meer open, en dan regenereert een volgende ronde de
deployment weer. Dat is juist: hij staat dan nog in het projectbestand (zie hieronder),
dus het cluster volgt het projectbestand.

## Oorzaak B: stap 8 liep door als stap 2 mislukte

Stap 2 vangt elke fout af en zet hem in `deletion_results["errors"]`. Daarna liep de flow
door en haalde stap 8 de deployment uit het projectbestand, ook als het argo-bestand er
nog stond. Gemeten bij `pm-5sj/pr139` op 2 september: de push van stap 2 faalde met
"Cannot push to main ... Remote has conflicting changes that cannot be automatically
merged", en om 16:48:00 haalde stap 8 de deployment toch weg. De taak eindigde als
`failed`, maar een retry kon niets meer: zonder de deployment geeft `delete_deployment`
404.

### Wat er nu gebeurt

Stap 8 draait alleen als git echt leeg is voor deze deployment: het argo-bestand weg en
gepusht (stap 2), en de manifestmap weg en gecommit (stap 7). Anders blijft de deployment
in het projectbestand staan, faalt de taak met de fout van stap 2 of 7, en kan een retry
het opnieuw proberen. De overgeslagen stap komt als operatie terug met
`status: "skipped"` en de reden erbij.

RC-238 heeft daar een derde voorwaarde bij gezet, om een eigen reden: ook een cluster-delete
die niet BEVESTIGD is houdt stap 8 tegen (zie "De project file volgt het cluster" in
`features/argocd-vastgelopen-verwijdering.md`). Dat raakt de uitgestelde opruiming, die de
manifestmap bewust laat staan omdat de Application nog niet verifieerbaar weg is: die liep
voorheen door naar stap 8 en doet dat nu niet meer. De rij die de map terugvindt wordt nog
steeds geschreven, en de deployment blijft in het projectbestand staan, dus beide kanten
vragen nog om dezelfde stand en een retry kan het afmaken. Een mislukte git-bewerking blijft
een andere zaak: die laat een map achter die niemand meer kent. Het gedrag van force-mode
verandert hier niet.

## Waar het staat

| Wat | Waar |
|---|---|
| De query naar open delete-taken | `opi/core/async_task_service.py`, `find_open_delete_deployments` |
| Het overslaan in de verwerkingsronde | `opi/manager/project_manager.py`, `process_project` |
| Het overslaan in de ArgoCD-wacht | `opi/manager/project_manager.py`, `process_project_from_git` |
| De grendel voor stap 8 | `opi/manager/delete_project_manager.py`, `delete_deployment` |
| Tests | `tests/test_zombie_deployment.py` |

## De zombies die er al stonden

De drie uit productie zijn met de hand opgeruimd. Voor wat een verdwenen Application
achterlaat is er `task argocd-orphan-sweep`
(`operations-manager/python/scripts/argocd_orphan_sweep.py`).
