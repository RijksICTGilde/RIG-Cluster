# Status-afwijkingen: waarom een deployment niet groen is

## Wat het is

Naast fouten (`errors`) kent de deploymentstatus nu afwijkingen (`deviations`): resources die de status van Synced/Healthy afhouden zonder dat er iets stuk is, elk met een reden. Aanleiding was mb-docs-helmfile: alles draaide exact volgens git, maar twee oude Jobs hingen in verwijdering en de kaart toonde alleen twee gele badges zonder verklaring. Zie `plans/status-uitleg-bij-afwijking.md`.

## Waar je het ziet

- **ZAD-deploymentkaart**: bij een afwijking verschijnt onder de badges een lijst "Waarom niet groen" (max 5 regels, daarna "en N meer"). Bij de verwarrende combinatie sync OutOfSync met een geslaagde laatste sync-operatie staat er "Laatste sync geslaagd, maar N resources wijken nog af". Een gezonde kaart rendert exact als voorheen: geen ruis.
- **V2-API** (voor agents): `GET /api/v2/projects/{project}/deployments` en `.../deployments/{deployment}` bevatten `deviations: [{resource, kind, reason}]`, gevuld in dezelfde probleemstatussen als `errors`. Een OutOfSync-deployment met lege `errors` en alleen deviations draait gewoon goed; de deviations vertellen wat er nog afwijkt.

## Redenen

- `is verwijderd, maar het cluster maakt de verwijdering niet af`: de laatste geslaagde sync heeft de resource al als Pruned gemeld maar hij bestaat nog (bijv. een vastgelopen finalizer); opnieuw syncen lost dit niet op.
- `staat niet meer in git en wordt bij de volgende sync opgeruimd`: prune moet nog gebeuren.
- `wijkt af van git en wordt bij de volgende sync bijgewerkt` / `wijkt af van git; auto-sync staat uit`: een gewone diff.
- `nog bezig`: resource met health Progressing zonder message (mét message staat hij al bij de fouten).

## Implementatie

`gather_sync_deviations()` in `opi/services/deployment_diagnostics.py` leest puur de al opgehaalde ArgoCD Application-payload (geen extra API-calls). Resources van uitgeschakelde componenten worden gefilterd, zoals bij de fouten. Consumenten: `_fetch_argocd_deployment_status` (web) en `_fetch_one_live_status` (V2, via `DeploymentDetail.deviations`).
