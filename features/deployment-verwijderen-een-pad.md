# Een deployment verwijderen: één pad

Een deployment verwijderen had zeven ingangen en twee volledige implementaties: een
expliciete delete (922 regels) en de opruiming van een deployment die uit het projectbestand
verdween (548 regels). Ze verschilden in volgorde, retentie, welke services ze aanraakten,
hoe ze een vastgelopen ArgoCD-applicatie behandelden en hoe de uitkomst tot stand kwam.
Sinds RC-484 lopen ze door dezelfde use case.

## Waar het pad loopt

```
ingang (route of task handler)
   |  vertaalt invoer naar een DeploymentRemoval, en uitkomst of exception naar zijn antwoord
   v
opi/manager/project/
   deployment_removal.py  remove_deployment(DeploymentRemoval) -> DeploymentRemovalResult
                          DeploymentRemovalError, ProjectNotFoundError, DeploymentNotFoundError
                          find_removable_deployment, marking_service
   argocd_files.py        de ArgoCD-bestanden van een project in de GitOps-repo
   |
   v
de managers per service, de kubectl-connector en de drie git-werkmappen
```

Welke ingang hoe binnenkomt:

| ingang | hoe |
|---|---|
| v1 `DELETE /api/projects/{p}/{d}` | 404-controle, dan de taak `delete_deployment`, 202 |
| v1 `DELETE /api/projects/{p}/{d}?sync=true` | dezelfde taak, in het verzoek uitgewacht: 200, of 207 als er iets bleef staan |
| v2 `DELETE /api/v2/projects/{p}/{d}` | 404-controle, dan dezelfde taak, 202 |
| web `POST /projects/{p}/delete-deployment/{d}` | 404-controle, dan dezelfde taak, met voortgangsvenster |
| task handler `handle_delete_deployment` | `remove_deployment`, trigger `EXPLICIT`, `force=True` |
| project verwijderen | `delete_deployment` per deployment, trigger `EXPLICIT` |
| projectbestand gewijzigd | `remove_deployment`, trigger `PROJECT_FILE_EDIT`, `force=True` |

De synchrone ingang loopt sinds RC-484 óók door de taakrij. Daarmee gelden de taakordening,
de dedup, de drain guard en de grendel die een rollout tegenhoudt zolang er een open
delete-taak voor dezelfde deployment staat (RC-254) ook voor hem.

## De stappen, in deze volgorde

1. **Zoeken.** De deployment, gefilterd op `CLUSTER_MANAGER`. Is hij er niet, dan
   `DeploymentNotFoundError`; onder `force` is dat `already_absent` en geen fout.
2. **ArgoCD-bestanden weg uit git.** De Application, zijn AppProject (alleen als niets er
   meer naar wijst) en de repository-secrets gaan in één mutatie, met de kustomization
   daarna herbouwd.
3. **Wachten tot de Application echt weg is.** Een `PermissionError` telt als verwijderd:
   de AppProject is dan eerder weg dan de Application. Onder `force` wordt een vastgelopen
   Application opgeruimd en wordt op zijn cascade gewacht.
4. **De services.** Keycloak, Redis, de database, MinIO, SeaweedFS en het mailaccount, elk
   via zijn eigen manager. Een service die omvalt laat de rest staan.
5. **De backupdata markeren**, zolang de namespace er nog staat: het Kopia-wachtwoord is
   daaruit afgeleid en gaat mee in de markering.
6. **De namespace.** Is er data gemarkeerd, dan wordt de namespace zelf ook gemarkeerd en
   haalt de purge hem later weg. Anders wordt hij verwijderd. Een `delete_namespace` die
   `False` teruggeeft is een fout, nooit "bestond niet".
7. **De manifesten.** Alleen als de Application bevestigd weg is; anders blijven ze staan en
   worden ze gemarkeerd voor een latere poging, want de finalizer heeft het bronpad nodig.
8. **De entry uit het projectbestand**, alleen bij een expliciete delete, en alleen als git
   en het cluster schoon waren. Anders blijft de entry staan zodat een herhaling hem kan
   afmaken.
9. **De subdomeinen.**
10. **Hermeten.** De Application, het AppProject en de namespace worden opnieuw opgevraagd.
    Die meting bepaalt de uitkomst, niet de foutenlijst: een fout die tijdens de rit waar was
    en aan het eind niet meer, beschrijft niets dat bleef staan.

## Retentie

Elke verwijdering markeert eerst. De grace period staat in
`DELETION_GRACE_PERIOD_DAYS` (7 dagen, `opi/core/config.py`) en de purge in
`opi/jobs/reconciliation.py` haalt het daarna weg. Alleen een onbereikbare database laat de
data meteen wissen, want hem ongemarkeerd laten staan zou hem lekken in plaats van bewaren.

Het veld `data-retention-period` bestaat niet meer. Het stond alleen in de pydantic-modellen,
niet in `project_v2.json`, dus een schemageldig bestand kon het niet zetten; het aantal uren
werd nergens gebruikt.

## Wat een ingang nog zelf doet

Alleen vertalen. De use case raist getypeerde excepties, en de adapter
`DeleteProjectManager.delete_deployment` zet ze om:

| exception | antwoord van de adapter |
|---|---|
| `ProjectNotFoundError`, `DeploymentNotFoundError` | `HTTPException` 404 |
| `DeploymentRemovalError` | `HTTPException` 500 |

De routes zelf zien die excepties niet: hun 404 valt al in `require_removable_deployment`,
en wat daarna misgaat rapporteert de taakhandler.

Onder `force` raist de use case niets: de uitkomst draagt de fouten, zodat een
projectverwijdering en de nachtelijke opruimer elke deployment aflopen.

De 404-controle vóór de taak staat in `require_removable_deployment`
(`opi/http/task_helpers.py`) en draait op elke ingang, zodat een onbekende naam in het
verzoek antwoordt en niet pas als taak die even later faalt.
