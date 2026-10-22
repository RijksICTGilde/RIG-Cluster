# Wachtwoorden roteren: het stappenplan

Hoe je een platformwachtwoord gecontroleerd roteert, per onderdeel of in bulk, met zo min
mogelijk downtime. De analyse staat in `plans/wachtwoorden-roteren-flow-analyse.md`, het
voorstel dat eraan voorafging in `plans/wachtwoorden-roteren-per-component.md`, en het
verloop zelf in `plans/wachtwoorden-roteren-logboek.md`. Dit document is het draaiboek dat
je volgt; die twee zijn de onderbouwing.

Alles loopt één ingang: `scripts/edit-secret.py` (module `scripts/secret_rotate.py`).

## 0. Begrippen

- **Categorie**: hoe het component zijn wachtwoord krijgt. Dat bepaalt de volgorde, niet andersom: `cnpg-secret` (postgres superuser en de managed roles keycloak-db / mail-db / forgejo-db / platform-backup: het bestand gaat eerst, de CNPG-operator past de rol dan zelf aan), `live-app` (redis / mail-relay / keycloak-mail / keycloak-admin: de app krijgt hem als eerste, het bestand volgt), `env-restart` (minio / prometheus / pgadmin / secret-key: lezen alleen bij start), `external` (transip / grafana / platform-repo-pat / keycloak-master-oidc / registry / argocd-webhook-github: wijziging loopt buiten het cluster).
- **Gates**: drie metingen die de ronde dragen — de Argo-sync die als `Succeeded` terugkomt, het cluster-secret dat de nieuwe waarde draagt (poll met timeout), en bij cnpg de operator-reconcile (oude login faalt, nieuwe werkt). Pas daarna herstart een consumer.
- **Dekkingspoort**: de tool meet vóór elke ronde en in CI dat elk geheim een rotatieweg heeft. Dat geldt voor de templates onder `infrastructure/bootstrap/infrastructure/secrets/templates/` en voor het envsecret van ZAD (`bootstrap/rig-system/kustomize/operations-manager/overlays/<cluster>/operations-manager-env-secrets.yaml`): elke sleutel daarin is een rotatierij, staat in `ZAD_ENV_CONFIG` (configuratie, geen geheim) of in `ZAD_ENV_NON_ROTATABLE` (een benoemde reden). Een naam die alleen nog in die twee lijsten staat is ook een gat, zodat ze niet stil verouderen. De poort meet de envsecrets die in git staan: het bestand in de sandbox-overlay schrijft `task sandbox:generate-env-secrets` lokaal uit een eigen `.env`, en die inhoud verschilt per machine.
- **Keyring**: de AGE-sleutels komen uit `SOPS_AGE_KEY[_FILE]`, dan `~/.config/sops/age/keys.txt`, dan `security/*.txt` in deze repo. Het bestand is altijd leidend via zijn recipients; de uitvoer noemt welke bron leverde.

## 1. Voorbereiding (eenmalig per cluster)

De secrets-overlay van het cluster moet in deze repo bestaan. Check:

```bash
ls infrastructure/bootstrap/infrastructure/secrets/config/overlays/
```

Staat de map van je cluster (bijv. `sandboxed-local`) er niet bij, dan seed je hem eerst —
dat schrijft de live waarheid weg als SOPS-bestanden, zonder één waarde te wijzigen:

```bash
# preview (leest alleen):
uv run --project operations-manager/python python scripts/edit-secret.py --seed-overlay --context <kubectl-context>

# en dan echt schrijven:
uv run --project operations-manager/python python scripts/edit-secret.py --seed-overlay --apply --context <kubectl-context>
```

Daarna: review de diff, commit, push (op sandbox: `task sandbox:sync` brengt hem naar de
Forgejo-mirror). Controleer dat alles gelijk loopt:

```bash
uv run --project operations-manager/python python scripts/edit-secret.py --check --all --context <kubectl-context>
```

Elke component moet zonder DRIFT rapporteren. Dat is meteen je dagelijkse drift-kaart:
draai hem wanneer je wilt weten of bestand, cluster en app nog gelijk lopen.

## 2. Eén component roteren

Altijd eerst het plan, dan echt:

```bash
# 1. het plan, met per stap de commando's:
uv run --project operations-manager/python python scripts/edit-secret.py --component postgresql --context <kubectl-context>

# 2. echte rotatie (vraagt per stap bevestiging; genereert de nieuwe waarde zelf waar de template dat kan):
uv run --project operations-manager/python python scripts/edit-secret.py --component postgresql --apply --context <kubectl-context>
```

De ronde zelf: pre-flight (werkt de huidige waarde nog tegen de app?) → bestand bijwerken →
**git committen en pushen (de enige mensenstap: de reviewpoort)** → de tool refresht en synct
de eigenaar-Applicatie zelf, wacht op `Succeeded` én op het cluster-secret → bij
cnpg-componenten de operator-poort → consumer-restarts → verificatie. Een component waarvan
het overlay-bestand nog plaintext is (het geval `keycloak-db-credentials.yaml` op odcn)
krijgt in dezelfde ronde zijn migratie: canoniek versleuteld, bedrading omgelegd, plaintext weg.

Faalt iets halverwege: de toon zegt waar, en waar de oude stand bereikbaar bleef. Beginnen
opnieuw kan altijd; pre-flight en de drift-vraag houden je eerlijk.

## 3. Alles in één ronde (bulk)

```bash
uv run --project operations-manager/python python scripts/edit-secret.py --all --context <kubectl-context>          # plan
uv run --project operations-manager/python python scripts/edit-secret.py --all --apply --context <kubectl-context>  # echt
```

Volgorde is de tabelvolgorde en die is afgemeten: de database-superuser voorop (niemand hangt
ervan af), de declaratieve database-rollen daarna, live-apps, env-restarts, externe sleutels,
en `keycloak-admin` als allerlaatste — dat is het enige wachtwoord waarmee je jezelf
buitensluit uit het systeem waarmee je herstelt. `--all` stopt bij de eerste breuk; een halve
ronde verder zou drift geven.

**Restarts in een bulkronde: één keer, gededupliceerd, aan het einde.** Elk component weet
welke workloads zijn waarde meelezen (de `workloads`-kolom in de tabel; ZAD staat er alleen in
als hij de waarde echt gebruikt). Bij meerdere componenten stelt de tool die herstarts uit naar
een slotafronding: de apps zelf eerst, ZAD als allerlaatste, nooit twee keer dezelfde pod.
Omdat een pod zijn omgeving alleen bij start inleest, is dat uitstel veilig; uitzondering om
op te letten: na de operator-gate van keycloak-db tot de slot-herstart werken bestaande
Keycloak-verbindingen, maar kunnen nieuwe logins even tegen de net aangepaste rol aanlopen.

## 4. Verwachte onderbrekingen

- postgresql / mail-db / forgejo-db: geen downtime van de database; consumer-restarts zijn kort.
- platform-backup: geen downtime en geen herstart; de nachtelijke CronJob leest het secret bij zijn volgende run. KOPIA_PASSWORD in dezelfde template roteert niet mee.
- keycloak-db: de database zelf blijft up; Keycloak herstart (login-onderbreking van seconden tot een halve minuut).
- minio/prometheus/pgadmin: de pod herstart kort. Bij prometheus herstart ook Keycloak, die
  hetzelfde token leest voor zijn rig-metrics-endpoint (dezelfde login-onderbreking als
  hierboven: seconden tot een halve minuut).
- redis / keycloak-admin / relay: geen herstart van de component zelf, alleen ZAD waar nodig.
- secret-key: ZAD herstart, en daarmee vervalt elke sessie en elk CSRF-token. Iedereen logt opnieuw in en een half ingevuld formulier is weg.
- keycloak-master-oidc: tussen het regenereren en de herstart van ZAD kan ZAD geen realm van een sso-rijk-provider voorzien.
- registry: geen onderbreking. Een nieuw robot-token komt NAAST het oude, dus het oude blijft geldig tot je het in de registry intrekt. Die intrekking is een eigen stap in de ronde, en zonder haar verandert de rotatie niets.

## 5. Guardrails die er expres in zitten

- Zonder `--apply` schrijft of wijzigt niets iets (bestanden noch cluster).
- `--apply` tegen een productiecluster (`prd`/`production` in de naam) vraagt de clusternaam over te typen.
- Bij een plaintext overlay-bestand volgt migratie; bij een secret zonder Argo-eigenaar stopt de sync-stap met de melding welk pad dat vraagt, in plaats van te wachten op een sync die niet komt.
- Elke bestandsschrijving bewaart de bestaande recipient-set en eindigt met een decrypt-proef.

## 6. Wat bewust handwerk blijft

De git commit+push (de reviewpoort op een diff aan geheimen), de TransIP-console (hun API kan zijn eigen sleutel
niet roteren), het aanvragen van een nieuwe Grafana-token bij ODCN, het regenereren van het clientgeheim in de
upstream Keycloak (`keycloak-master-oidc`), en voor `registry` zowel het aanmaken van een nieuw robot-token in
de registry-console als het intrekken van het oude. Voor de componenten die alleen in het envsecret van ZAD
leven blijft ook het regenereren van dat secret handwerk:
`task generate-env-secrets-for-operations-manager CLUSTER_TYPE=<cluster>`, daarna committen en pushen, en
daarna toepassen met `task deploy-operations-manager CLUSTER_TYPE=<cluster>` (op de sandbox:
`task sandbox:update-operations-manager`). Die laatste stap is nodig omdat de OPI-overlay in geen
ArgoCD-Application hangt: zonder de deploy-task blijft het nieuwe secret in git staan en draait OPI de oude
waarde. Bij `secret-key` wacht de tool daarna op het cluster-secret voor ze OPI herstart. De sops-age-sleutel zelf
roteren loopt apart, via `scripts/rotate-sops-key.py`.
