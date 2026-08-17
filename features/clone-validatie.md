# Kloon vooraf controleren (`:validate-clone`)

Een kloon schrijft in een levende database of bucket. Daarom is er een controle die niets
kloont en alleen zegt of het zou kunnen: `POST /api/projects/{project}/deployments/{deployment}/:validate-clone`.

## Gebruik

```bash
curl -X POST "https://zad.sandbox.rijksapp.dev/api/projects/mijn-project/deployments/productie/:validate-clone" \
  -H "X-API-Key: <projectsleutel>"
```

- **200** met `"status": "valid"` - de configuratie is compleet.
- **422** met `"status": "invalid"` - er ontbreekt iets; `validation.checks` zegt per controle wat.

```json
{
  "status": "invalid",
  "message": "Clone validation failed for productie",
  "project": "mijn-project",
  "deployment": "productie",
  "validation": {
    "passed": false,
    "checks": [
      {"name": "clone_configuration", "status": "success", "message": "Clone configuration found: type=deployment, mode=once"},
      {"name": "source_deployment_exists", "status": "failed", "message": "Source deployment 'verdwenen' not found in project"}
    ]
  }
}
```

## Wat er gecontroleerd wordt

Alles wat een kloon nodig heeft om te *kunnen*, staat in het projectbestand. De controle is dan ook
een pure functie daarover (`opi/manager/clone_validation.py`): geen git, geen cluster, geen connectors.

| Controle | Wanneer | Faalt als |
|---|---|---|
| `deployment_exists` | altijd | de deployment staat niet in het project |
| `clone_configuration` | altijd | er is geen `clone-from`, of het is geen mapping |
| `clone_pending` | `mode: once` die al gedraaid heeft | nooit - dit is een melding, geen fout (een nieuwe kloon vraagt om force-clone) |
| `source_deployment_exists` | `type: deployment` | de bron staat niet in het project, of is de deployment zelf |
| `remote_source_exists` / `chisel_configuration` / `services_configuration` | `type: remote-source` | de remote-source ontbreekt, heeft geen chisel `server-url`, of configureert geen diensten |
| `backup_items` | `type: backup` | er zijn geen items, of een item mist `resource_type` of `snapshot_id` |
| `clone_type` | onbekend type | het type is geen `deployment`/`remote-source`/`backup` |

Bereikbaarheid van een externe bron wordt bewust **niet** getoetst: dat vraagt een tunnel en
credentials, en dan is het geen droge controle meer.

## Geschiedenis

Het endpoint riep `project_manager._clone_manager` aan. `clone_manager.py` is in december 2025
verwijderd; het endpoint bleef achter en gaf sindsdien
`'ProjectManager' object has no attribute '_clone_manager'` als 500. De twee tests erop mockten
juist dat attribuut en bleven daardoor groen. Sinds RC-77 draaien die tests tegen de echte controle
(`tests/test_clone_validation.py` en `tests/integration/test_project_api.py::TestValidateCloneEndpoint`).
