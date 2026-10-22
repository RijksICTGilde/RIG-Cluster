# Kloon vooraf controleren (`:validate-clone`)

Controleer of een kloon kan draaien zonder hem werkelijk uit te voeren: `POST /api/projects/{project}/deployments/{deployment}/:validate-clone`.

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

## Dezelfde toets in het provisioningpad

Beide doelen (validatie-endpoint en provisioning-managers) gebruiken dezelfde functie,
`resolve_clone_source_deployment` in `opi/manager/clone_validation.py`. Dit voorkomt dat
een reference die validatie passeert later toch faalt, en dat een reference op twee
verschillende manieren wordt opgelost.

De functie weigert een reference die leeg is, naar de deployment zelf wijst, of geen
deployment van dit project is. Drie provisioningpaden roepen hem aan voordat ze een
bronnaam samenstellen:

| Pad | Waar |
|---|---|
| database | `database_manager._ensure_database_state` |
| buckets | `minio_manager.clone_minio_from_deployment` |
| volumes | `pvc_manager` |

De schrijfkant gebruikt dezelfde functie: `upsert_deployment` zet een `clone-from` met
`force_clone` alleen neer als de reference een deployment van dit project is, en weigert
het verzoek anders met `success: false`.

Dezelfde module houdt ook de botsing tegen die niet over klonen gaat: een samengestelde
resourcenaam die bij een ander project uitkomt. Zie
[samengestelde-naambotsing.md](samengestelde-naambotsing.md).

Een afgeronde kloon (`mode: once`, `completed`) skipt klonen al eerder. Een projectbestand
dat naar een verwijderde bron verwijst blijft uitrollen; alleen een kloon die echt zou
lopen, en niet kan, faalt nu.

