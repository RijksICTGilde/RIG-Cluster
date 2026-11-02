# Credentials van een deployment vervangen

Een projectbeheerder vervangt met één API-aanroep het wachtwoord, de secret key of het
clientsecret dat een deployment voor een platformdienst gebruikt. OPI maakt de nieuwe
waarde zelf, zet hem in de dienst en schrijft hem in het Secret van de deployment. De
aanroeper geeft nooit een waarde mee en krijgt er nooit een terug.

## De route

```
POST /api/v2/projects/{project_name}/deployments/{deployment_name}/:rotate-credentials
X-API-Key: <de API-key van het project>

{"services": ["postgresql-database", "minio-storage", "keycloak"]}
```

Het antwoord is een 202 met een taak-id, net als `:refresh`. Poll
`/api/tasks/{task_id}`; het resultaat noemt in `rotated` de diensten waarvan het geheim
is vervangen.

| Antwoord | Wanneer |
|---|---|
| 202 | De aanvraag is aangenomen, de taak staat klaar |
| 401 | Geen of een verkeerde `X-API-Key` |
| 404 | Het project bestaat niet |
| 422 | Een dienst die deze deployment niet gebruikt (het bericht noemt de diensten die wel kunnen), of een dienst waarvan een lopende `clone-from` het geheim bepaalt |

`rollout=false` wordt geweigerd: de nieuwe waarde bereikt de pods alleen via de uitrol.

## Wat er per dienst gebeurt

| Dienst | Wat wordt vervangen |
|---|---|
| `postgresql-database` | Het wachtwoord van de databaserol van de deployment en dat van de read-only rol `<rol>_ro`. Werkt ook als de database per namespace draait (`namespace-postgresql-database`) |
| `minio-storage` | De secret key. De MinIO-gebruiker wordt verwijderd en opnieuw aangemaakt, de bucket en de rechten blijven |
| `keycloak` | Het clientsecret van de deployment-client, door Keycloak zelf opnieuw gemaakt. De `client_id` blijft |

De rotatie loopt mee in de gewone verwerking van de deployment: de managers maken een
nieuwe waarde in plaats van de opgeslagen te hergebruiken. Daarna schrijft OPI het
Secret, en de pods herstarten via de hash die de CMP over alle Secrets en ConfigMaps
zet.

## Een korte onderbreking hoort erbij

Tussen het moment dat de dienst de nieuwe waarde heeft en het moment dat de pods met die
waarde draaien, werkt de oude waarde niet meer. Een draaiende pod loopt in die periode
tegen authenticatiefouten aan. Overlap zonder onderbreking (twee geldige waarden
tegelijk) zit hier niet in: dat vraagt per dienst een tweede account en is een eigen
ontwerp.

Faalt de stap die het Secret wegschrijft, dan blijft die onderbreking staan: de dienst
heeft de nieuwe waarde al, `zad-deployments` nog de oude, en de taak eindigt op `failed`.
Herhaal de aanroep zodra de oorzaak weg is.

## Wat deze route niet doet

- Niet het API-key van het project zelf, en niet het realm-adminaccount van het project.
- Niet een deployment met een externe Keycloak: dat clientsecret wordt buiten ZAD
  beheerd, en een aanvraag ervoor faalt met een duidelijke melding.
- Niet een dienst waarvan een `clone-from` nog loopt. De klooproute bepaalt zelf welke
  waarde de deployment krijgt en komt niet langs de rotatie: bij een externe bron
  (`type: remote-source`) geldt dat voor de database en de bucket, bij een kloon van een
  andere deployment (`type: deployment`) voor de bucket. De aanvraag wordt daarom
  geweigerd met 422 in plaats van stil overgeslagen. Een afgeronde `mode: once`-kloon
  blokkeert niets meer, tenzij `force-clone` aanstaat en de kloon dus opnieuw loopt, en
  `type: backup` nooit.
- Niet meerdere deployments tegelijk. Eén aanroep per deployment.

## Toetsen in de sandbox

Er is geen e2e-test die een deployment met alle drie de diensten opbouwt, dus de
acceptatie gaat met de hand. Lees de huidige waarden, roep de route aan, en toets per
dienst dat de oude waarde wordt geweigerd en de nieuwe wordt toegelaten.

Kies een project waarvan de repository credentials heeft. Zonder die credentials strandt
de git-stap, en dan rouleren de diensten wel maar blijven de secrets in de deploymentmap
oud staan, zodat je de nieuwe waarde niet kunt lezen.

De waarden staan SOPS-versleuteld in de deploymentmap van `zad-deployments`
(`<cluster>/<project>/<deployment>/<deployment>-{database,minio,keycloak}-secret.sops.yaml`),
versleuteld met de AGE-sleutel van het project. Die sleutel komt uit twee slagen: de
clustersleutel (`kubectl get secret sops-age-key -n rig-system`) opent
`config.age-private-key` in het projectbestand, en dat opent zowel `config.api-key` als de
secrets in de deploymentmap.

De drie probes, elk vanaf een pod die de dienst kan bereiken:

```bash
# PostgreSQL, hoofdrol en read-only rol
kubectl exec -n rig-system rig-db-1 -c postgres -- \
  env PGPASSWORD="<waarde>" psql -h rig-db-rw -U <rol> -d <database> -tAc "select 1"

# MinIO (mc staat in de OPI-pod; die heeft een schrijfbare config-dir nodig)
kubectl exec -n rig-system deployment/operations-manager -- sh -c \
  "mc --config-dir=/tmp/p alias set p http://minio.rig-system.svc.cluster.local:9000 <gebruiker> '<waarde>' && mc --config-dir=/tmp/p ls p/<bucket>"

# Keycloak clientsecret
curl -s -X POST "<OIDC_URL>/realms/<realm>/protocol/openid-connect/token" \
  -d grant_type=client_credentials -d client_id=<client> --data-urlencode "client_secret=<waarde>"
```

Daarna laat `kubectl -n rig-<project> get pods` herstarte, gezonde pods zien. Op een
sandbox waar ArgoCD geen Application voor de deployment aanmaakt draaien er geen pods en
is die laatste stap niet te meten; de drie probes hierboven wel, want die gaan langs de
dienst en niet langs de pod.

## Waar het staat

- Route: `opi/api/v2/router.py`, `rotate_credentials_v2`
- Welke diensten kunnen: `opi/services/credential_rotation.py`
- Taak: `handle_rotate_credentials` in `opi/core/task_handlers_operations.py`
- Rotatie zelf: `DatabaseManager._resolve_database_credentials`,
  `MinioManager.create_resources_for_deployment`,
  `KeycloakManager._setup_sso_rijk_integration` en
  `KeycloakConnector.regenerate_client_secret`
