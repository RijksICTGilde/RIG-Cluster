# Een project als geheel opvragen

De v2-API kon een project wél veranderen maar niet tónen. Je kon een component toevoegen
(`POST /projects/{naam}/components`) zonder te kunnen opvragen welke componenten er al
waren, en een dienst configureren zonder te kunnen zien welke diensten het project
gebruikt. Wie wilde weten hoe een project eruitzag, moest het antwoord al kennen.

Drie leesendpoints vullen dat gat. Ze zijn geen alternatieven maar lagen: de twee delen
zijn los bruikbaar, en het geheel is de samenstelling ervan plus de bestaande
deploymentlezer. Per gegeven is er zo één waarheid.

| Endpoint | Antwoord |
|---|---|
| `GET /api/v2/projects/{naam}/services` | Welke diensten het project gebruikt, per laag, met de configuratie waar die staat |
| `GET /api/v2/projects/{naam}/components` | De componentdefinities: poorten, routering, resources, bindingen, env-var-namen, aliassen, bijlagekoppelingen |
| `GET /api/v2/projects/{naam}` | Bovenstaande twee plus de deployments (mét draaistatus) en `pending_rollout` |

Alle drie vragen de **projectsleutel** in de `X-API-Key`-header, net als de rest van
`/api/v2/projects/{naam}/...`.

## Gebruik

```bash
curl "https://zad.rijksapps.nl/api/v2/projects/mijn-project" \
  -H "X-API-Key: $ZAD_PROJECT_KEY"
```

```json
{
  "project": {
    "name": "mijn-project",
    "display_name": "Mijn Project",
    "description": "Aangemaakt via de portal",
    "clusters": ["odcn-production"]
  },
  "source": "project-file",
  "cluster": "odcn-production",
  "pending_rollout": {
    "project": "mijn-project",
    "count": 2,
    "since": "2026-08-09T10:00:00Z",
    "task_types": ["configure_service"]
  },
  "services": [
    {
      "name": "publish-on-web",
      "usages": [
        {"target": "project", "component": null, "deployment": null, "config": null},
        {"target": "component", "component": "backend", "deployment": null,
         "config": {"tls": "standard"}}
      ]
    }
  ],
  "components": [
    {
      "name": "backend",
      "type": "single",
      "ports": {"inbound": [8000], "outbound": [443]},
      "path": [{"match": "/api"}],
      "resources": {"cpu": "1", "limits": {"memory": "649Mi"}},
      "services": ["publish-on-web", "keycloak"],
      "env_var_names": ["API_TOKEN", "DATABASE_PASSWORD"],
      "aliases": {"POSTGRES_HOST": "$DATABASE_SERVER_HOST"},
      "attachments": [
        {"reference": "server-cert", "provide_as": "file",
         "path": "/etc/ssl/cert.pem", "env_name": null}
      ]
    }
  ],
  "deployments": [
    {
      "name": "production",
      "project": "mijn-project",
      "cluster": "odcn-production",
      "namespace": "mijn-project",
      "subdomain": "production",
      "components": [{"reference": "backend", "image": "ghcr.io/org/backend:1.0"}],
      "urls": {"backend": "https://production.rijksapps.nl"},
      "status": "Healthy",
      "sync_revision": "abc123def456789",
      "last_synced_at": "2026-08-10T12:00:00Z",
      "errors": []
    }
  ]
}
```

## Twee soorten status, allebei in het antwoord

**Draaistatus** — de deployments komen als volledige `DeploymentDetail` terug: `status`,
`sync_revision`, `last_synced_at` en `errors` (met categorie en uitleg) horen erbij. Een
overzicht met alleen namen, images en URL's ziet er compleet uit en zegt niets over of
het draait.

**Opbouwstatus** — welke componenten er zijn, en welke diensten gebruikt worden met hun
configuratie op de laag waar die staat.

## De vier lagen blijven apart

Een dienst kan op vier plaatsen geconfigureerd staan, en elk voorkomen komt apart terug
met zijn `target` plus de bijbehorende `component`/`deployment`:

| `target` | Betekenis |
|---|---|
| `project` | Geldt voor het hele project |
| `component` | Geldt voor dat component |
| `deployment` | Geldt voor die deployment |
| `deployment-component` | Geldt voor dat component binnen die deployment |

Platslaan tot "dienst X met configuratie Y" is bewust níét gedaan: dan is niet meer te
zien of `tls: standard` voor het project geldt of voor één component.

Het gelaagde `services`-blok is de **enige autoriteit** over configuratie. De kale
namenlijst bij een component (`components[].services`) is een kruisverwijzing en draagt
geen eigen configuratie.

Een dienst die kaal geselecteerd is (`- publish-on-web`, zonder config) komt terug met
`config: null`: hij staat aan. Dat is het punt waarop deze lezer bewust afwijkt van
`GET /projects/{naam}/services/{dienst}/config`, dat de vraag "waarmee is deze dienst
geconfigureerd" beantwoordt en een kale selectie daarom overslaat.

## Wat er nooit in het antwoord staat

Env-vars staan AGE-versleuteld opgeslagen; zelfs hun namen zijn er niet uit te halen
zonder te ontsleutelen. Dat gebeurt dus, en de waarden gaan daarna nergens heen.

| Inhoud | In het antwoord |
|---|---|
| env-var-namen | ja, gesorteerd |
| env-var-waarden | nooit |
| aliassleutels | ja |
| aliaswaarden | alleen als ze plain zijn opgeslagen, anders `"***"` |
| bijlagekoppeling (`reference`, `provide-as`, `path`, `env-name`) | ja |
| bijlage-inhoud (de catalogus onder `services{attachments}/data`) | nooit |
| dienstconfiguratie met een opgeslagen geheim (AGE-blok, `base64+age:`, `plain:`) | als `"***"` |
| `config/api-key`, `age-private-key`, `age-public-key` | nooit |

`env_var_names: null` betekent "niet te lezen", niet "geen variabelen". Een leeg lijstje
zou beweren dat we gekeken hebben en niets vonden.

De aliasregel houdt het bruikbaar: een alias die naar `$DATABASE_SERVER_HOST` wijst is de
hele reden dat je hem opvraagt.

## Het projectbestand is de bron, niet het cluster

Elk antwoord bevat `"source": "project-file"` en een `pending_rollout`-blok. Alles komt
uit `get_project_store()`, dus uit het projectbestand in de projectrepository. Wie met
`rollout=false` heeft opgeslagen, loopt met zijn bestand vooruit op het cluster; een
`count` groter dan 0 betekent dat deze beschrijving vooruitloopt op wat er draait.

## Eén ontsleutelpad

Het lezen van `user-env-vars` zat in de webrouter, voor de projectdetailpagina. Het staat
nu in `opi/services/project_env_vars.py` (`read_user_env_vars`) en zowel de pagina als de
API gebruiken die ene functie. Twee kopieën van een ontsleutelpad lopen uit elkaar, en
een uiteengelopen ontsleutelpad is hoe een waarde belandt waar een naam bedoeld was.

## Afhankelijkheden

Geen nieuwe. De endpoints gebruiken `get_project_store()`, de bestaande AGE-helpers, de
bestaande deploymentlezer en `task_service.get_deferred_rollouts()`.

## Buiten scope

`zad project describe` zelf: dat is de CLI-kant en hoort in de `zad-cli`-repo. Ook is er
bewust geen schrijfoppervlak bijgekomen voor `aliases` en `attachments` — dat is een eigen
ontwerpvraag over validatie en goedkeuring.

## Code en tests

- `opi/api/v2/project_read.py` — de verzamelaars en de responsmodellen
- `opi/api/v2/router.py` — de drie endpoints
- `opi/services/project_env_vars.py` — het gedeelde leespad voor env-vars
- `tests/test_project_read_api.py` — o.a. een test die het hele antwoord doorzoekt op de
  bekende plaintextwaarden, en een test die faalt zodra een veld van `DeploymentDetail`
  uit het samengestelde antwoord verdwijnt
