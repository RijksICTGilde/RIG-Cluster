# Back-up van de platformdatabase

## Wat het is

Een CronJob naast `rig-db` die elke nacht een logische dump maakt van de databases van het
platform zelf, versleuteld via Kopia naar de bestaande back-upbestemming. Het back-upsysteem
van OPI beschermt de gegevens van projecten; dit beschermt de gegevens waarmee het platform
zelf werkt.

Wat er in zit, en waarom dat niet opnieuw te maken is:

| Database | Wat erin staat |
|---|---|
| `keycloak` | De realms, clients en de lokale accounts die via uitnodigingen zijn aangemaakt. Die accounts staan nergens anders. |
| `operations_manager` | De gebruikers, het subdomeinregister, de runs en de verwijdermarkeringen van OPI. |
| `mailrelay` | De accounts en de wachtrij van de mailrelay (Stalwart), die geen PVC gebruikt. |
| `forgejo` | Alleen op de sandbox: daar is Forgejo de GitOps-bron. Op productie staat die database niet op dit cluster en slaat de run hem over. |

De projectdatabases op hetzelfde cluster staan hier NIET in: die hebben hun eigen back-up via
OPI (`features/backup-system.md`).

## Hoe het werkt

```
02:30 Europe/Amsterdam
  |
  CronJob rig-db-platform-backup (namespace van het platform)
  |
  +-- pg_dump -Fc per database --> emptyDir /dump
  |     leest met de rol platform_backup (pg_read_all_data)
  |
  +-- kopia snapshot per database --> bucket rig-platform-db
  |     identiteit <database>@rig-db, eigen bewaarregel per database
  |
  +-- kopia maintenance run --full
```

De CronJob staat in `infrastructure/bootstrap/infrastructure/postgresql/database/base/platform-backup-cronjob.yaml`
en komt dus mee in beide overlays. Hij loopt los van OPI: een storing of een stilstaande OPI
raakt deze back-up niet.

Per database een eigen Kopia-identiteit (`<database>@rig-db`) en dus een eigen snapshotlijst.
Daarmee is herstel per database mogelijk zonder de andere drie aan te raken.

De dump in de `emptyDir` is onversleuteld, dus hij staat er zo kort mogelijk: na de snapshot
weg, en via een `trap` ook als de run afbreekt. Anders zou een mislukte run zijn dump op de
node laten staan zolang de pod in de historie blijft. De pod krijgt geen API-token
(`automountServiceAccountToken: false`): hij praat alleen met postgres en met de bestemming.

`concurrencyPolicy: Forbid`: liever een gemiste nacht dan twee runs die elkaars dump
overschrijven. `backoffLimit: 1`, dus één herkansing, en `activeDeadlineSeconds: 3600`.

### De image

`ghcr.io/minbzk/base-images/rig-backup`, vastgezet op een digest. De image wordt alleen als
`latest` gepubliceerd, dus een digest om een nachtelijke taak vast te zetten. Een nieuwe
image komt hier pas in als iemand de digest bijwerkt.

### Credentials

Geen geheim staat als `value` in de podspec en geen geheim gaat door argv:

| Wat | Waar het staat | Hoe het gelezen wordt |
|---|---|---|
| Databaserol | `platform-backup-credentials` (`username`, `password`) | `PGUSER` en `PGPASSWORD` via `secretKeyRef` |
| Kopia-repository | `platform-backup-credentials` (`KOPIA_PASSWORD`) | kopia leest `KOPIA_PASSWORD` uit de omgeving |
| S3-bestemming | `operations-manager-env-secrets` (`BACKUP_S3_ENDPOINT`, `BACKUP_S3_ACCESS_KEY`, `BACKUP_S3_SECRET_KEY`) | kopia leest `AWS_ACCESS_KEY_ID` en `AWS_SECRET_ACCESS_KEY` uit de omgeving, mc zijn bestemming uit `MC_HOST_<alias>` |

De S3-sleutels komen uit het geheim dat OPI ook gebruikt, zodat er één plek is om de
bestemming te wisselen. `platform-backup-credentials` wordt per cluster gegenereerd uit
`infrastructure/bootstrap/infrastructure/secrets/templates/platform-backup-secret.yaml`
(`task generate-infrastructure-secrets-for-cluster`), zoals elk ander platformgeheim.

**Raak je `KOPIA_PASSWORD` kwijt, dan zijn deze back-ups onleesbaar.** Hij staat daarom in
hetzelfde SOPS-bestand als het wachtwoord van de leesrol: één bestand per cluster om te
bewaren.

Het wachtwoord van de leesrol roteert mee in de gewone ronde: `platform-backup` staat als
`cnpg-secret` in de tabel van `scripts/secret_rotate.py`, zonder herstart, want de CronJob
leest het secret bij zijn volgende run. `KOPIA_PASSWORD` roteert daar niet mee: een nieuwe
waarde maakt de bestaande snapshots onleesbaar in plaats van ze om te sleutelen, dus die
wissel hoort bij een nieuwe repository.

### De leesrol en niet de superuser

`platform_backup` is een managed role in `cluster.yaml` met alleen `pg_read_all_data`. De pod
kan dus niets wijzigen in de databases die hij leest. Dat is bewust anders dan het
superuser-secret dat er al stond: een nachtelijke pod met schrijfrechten op alle
platformdatabases is een staand risico, en lezen is alles wat een dump nodig heeft.

Komt de rol een object tegen dat hij niet mag lezen, dan faalt `pg_dump` zichtbaar in plaats
van een halve dump af te leveren. Nagemeten op PostgreSQL 17 met deze image: een dump door
deze rol van een database die hij niet bezit is volledig, en `pg_restore` zet alle rijen
terug.

### Bewaarregel

`kopia policy set` per database: 14 dagelijks, 8 wekelijks, 6 maandelijks, en `keep-latest`
ook op 14 zodat twee runs op één dag elkaar niet opruimen. Daarna `kopia snapshot expire
--delete`.

`kopia maintenance run --full` sluit de run af. Zonder dat maakt het verlopen van een
snapshot geen ruimte vrij: de manifests gaan uit de lijst, de blobs blijven in de bucket.
Het onderhoud hangt aan een eigen Kopia-identiteit (`platform-backup@rig-db`) en niet aan de
podnaam, want die is elke nacht anders en dan draait het onderhoud nooit.

### Persoonsgegevens, en hoe lang ze blijven staan

De dump bevat persoonsgegevens: de accounts in `keycloak` (e-mailadres en naam), in
`operations_manager` de tabel `users` (`email`, `full_name`) en de `created_by` van het
subdomeinregister en van de taken, en in `mailrelay` de wachtrij. De bewaarregel hierboven
bepaalt hoe lang ze terug te zetten zijn: **zes maanden** na de nacht waarin ze gedumpt zijn,
want dat is de langste regel. Een gebruiker die uit het platform verwijderd wordt staat tot
zes maanden later nog in een snapshot.

Fysiek uit de bucket zijn ze later dan dat. De bucket heeft versioning (zie hieronder), dus
de versleutelde blob die `kopia maintenance` na die zes maanden weghaalt blijft nog tot 210
dagen als oudere objectversie staan, tot de lifecycle-regel hem opruimt. Wie de S3-sleutel en
het Kopia-wachtwoord heeft, haalt die versie er met zijn version ID nog uit. De bewaartermijn
in de opslag is dus uiterlijk ongeveer dertien maanden, niet zes. Dat staat ook in
`docs/operations/backup-beleid.md` paragraaf 4, waar de bewaartermijnen van het platform bij elkaar
staan.

### De bucket en object lock

De run maakt `rig-platform-db` aan met `mc mb --with-lock`. Object lock is in S3 en in MinIO
alleen bij het aanmaken van een bucket aan te zetten, dus die keuze valt nu of nooit.
Nagemeten tegen MinIO `RELEASE.2025-09-07` met mc `RELEASE.2025-08-13T08-35-41Z`, dezelfde
release als in deze image: op een bestaande bucket antwoordt `mc retention set` met "Object
Lock configuration cannot be enabled on existing buckets".

Er staat GEEN default retention op de bucket. Dat is bewust: met een vergrendelde termijn op
elk object kan `kopia maintenance` zijn eigen verlopen blobs niet meer opruimen, en welke
modus en welke termijn het worden is een open beslissing (`TODO.md`, onveranderlijke
back-upopslag). Wat hier nu staat is dus de mogelijkheid en niet de bescherming: wordt die
termijn later gezet, dan hoeft de bucket niet opnieuw.

Object lock zet versioning aan, en dat heeft een gevolg dat anders stil doorgroeit: elke blob
die `kopia maintenance` weghaalt blijft als oudere versie in de bucket staan. De run zet
daarom eenmalig een lifecycle-regel die oudere versies na 210 dagen opruimt, ruim na de
langste bewaarregel (6 maandelijkse momenten, ongeveer 185 dagen). Een oudere versie bestaat
alleen van een blob die Kopia al had laten vallen, dus die marge is er voor de zekerheid en
niet omdat 185 dagen te kort zou zijn. De regel komt er alleen als de bucket nog geen
lifecycle-configuratie heeft: `mc ilm rule add` krijgt elke keer een nieuwe ID en zou zich
anders elke nacht opstapelen.

Een bucket `rig-platform-db` die al bestaat van voor deze wijziging heeft geen lock en krijgt
die ook niet: `mc mb --with-lock --ignore-existing` laat hem staan zoals hij is. Wil je de
lock daar wel, dan is het `mc rb` en de volgende run. Op productie bestaat de bucket nog
niet.

### Netwerk

De pod praat met `rig-db-rw` in zijn eigen namespace, wat het bestaande beleid al toestaat.
Naar de back-upbestemming staat een eigen regel in
`backup-destination/controller/overlays/odcn/network-policies/minio-networkpolicy.yaml`: die
MinIO laat alleen tenant-namespaces binnen, en deze CronJob draait in de platformnamespace.
De regel is smal gehouden op het podlabel `app.kubernetes.io/name: rig-db-platform-backup`,
niet op de hele namespace.

## Herstel van één database

Herstel is handwerk met de adminrechten, niet met de leesrol. Alles gebeurt in een pod met
dezelfde image, in de namespace van het platform.

```bash
DB=operations_manager

kubectl run herstel -n rig-system --rm -it --restart=Never \
  --image=ghcr.io/minbzk/base-images/rig-backup@sha256:3de60a9f38f9166a36e100561665eee86cc6a1723ccba5089803392b257a7e0c \
  --env=KOPIA_PASSWORD="$(kubectl get secret platform-backup-credentials -n rig-system -o jsonpath='{.data.KOPIA_PASSWORD}' | base64 -d)" \
  --env=AWS_ACCESS_KEY_ID="$(kubectl get secret operations-manager-env-secrets -n rig-system -o jsonpath='{.data.BACKUP_S3_ACCESS_KEY}' | base64 -d)" \
  --env=AWS_SECRET_ACCESS_KEY="$(kubectl get secret operations-manager-env-secrets -n rig-system -o jsonpath='{.data.BACKUP_S3_SECRET_KEY}' | base64 -d)" \
  --env=KOPIA_CONFIG_PATH=/tmp/kopia.config --env=KOPIA_CACHE_DIRECTORY=/tmp/kopia-cache \
  --env=KOPIA_LOG_DIR=/tmp/kopia-logs -- /bin/sh

kopia repository connect s3 --bucket=rig-platform-db --endpoint=<endpoint> --disable-tls \
  --override-hostname=rig-db --override-username=$DB

# 1. het herstelpunt kiezen
kopia snapshot list $DB@rig-db:/dump/$DB.dump

# 2. de dump terughalen
kopia snapshot restore <snapshot-id> /tmp/$DB.dump

# 3. terug in lege database
PGHOST=rig-db-rw PGUSER=postgres PGPASSWORD=<uit postgres-admin-credentials> \
  pg_restore --clean --if-exists --no-owner -d ${DB}_herstel /tmp/$DB.dump
```

Zet terug in een lege database en niet over de draaiende heen: dan is het resultaat te
controleren voordat er iets omgezet wordt. De database die het platform gebruikt wissel je
daarna bewust, en niet als bijwerking van een herstel.

Gemeten op de sandbox: zie "Gemeten" hieronder.

## Wat er niet in zit

- **Point in time recovery.** Dit is een dump per nacht, dus je verliest in het slechtste
  geval een dag. WAL-archivering via CNPG (`barmanObjectStore` of de Barman Cloud-plugin)
  hangt aan de CNPG-versie op ODCN en staat niet in deze repo; afgewogen en voor nu niet
  gedaan, omdat een logische dump per database onafhankelijk van de operator werkt en per
  database terug te zetten is.
- **De bestemming zelf.** Die staat vandaag op dezelfde opslaglaag als de productiedata; zie
  `docs/operations/backup-architectuur-overzicht.md`. Verhuist de bestemming, dan verhuist deze dump
  mee, want hij leest het endpoint uit hetzelfde geheim als OPI.
- **De rollen en wachtwoorden van PostgreSQL zelf.** Een logische dump per database bevat de
  globale objecten niet. De rollen komen declaratief uit `cluster.yaml` en hun wachtwoorden
  uit de SOPS-geheimen, dus die zijn uit git terug te zetten.

## Afhankelijkheden

- `rig-db` (CloudNativePG) met de managed role `platform_backup`
- `platform-backup-credentials` in de namespace van het platform
- `operations-manager-env-secrets` voor het endpoint en de sleutels van de bestemming
- de back-upbestemming (MinIO) met een bucket `rig-platform-db`, die de run zelf aanmaakt met object lock
- `ghcr.io/minbzk/base-images/rig-backup` (kopia, mc, PostgreSQL 17-client)

## Gemeten

Op de sandbox doorlopen op 16 oktober 2026, met de echte databases van dat cluster.

| Wat | Uitkomst |
|---|---|
| Een hele run, vier databases | 13 tot 15 seconden |
| De dumps | keycloak 12,9 MB, mailrelay 2,8 MB, forgejo 0,9 MB, operations_manager 250 KB |
| Twee runs achter elkaar | twee snapshots per database, de bewaarregel zoals bedoeld |
| Herstel van `operations_manager` | 2 seconden van snapshotkeuze tot `pg_restore` klaar |
| De herstelde database | dezelfde zeven tabellen met dezelfde rijaantallen als de draaiende |
| OPI op de herstelde database | start, alembic loopt schoon door, en `/api/v2/admin/marked-for-deletion` geeft dezelfde acht markeringen |

De run doet er zo weinig over dat de 3600 seconden van `activeDeadlineSeconds` ruim staan;
productie heeft meer data, maar niet in die orde.

De bucket met object lock is daarna apart nagemeten, tegen een MinIO in docker met dezelfde
mc-binary als in de image (sha256 `01f866e9...`, de som uit `images/mc/Dockerfile`):

| Wat | Uitkomst |
|---|---|
| `create_bucket` uit de CronJob, twee keer gedraaid | de bucket en de regel komen er een keer, de tweede keer verandert niets |
| De nieuwe bucket | versioning aan, een lifecycle-regel van 210 dagen, geen default retention |
| Een bucket zonder lock | "Object Lock configuration cannot be enabled on existing buckets", en ongeversioneerd |
| Kopia op de bucket met lock | repository aanmaken, snapshotten, `snapshot expire` en `maintenance run --full` lopen alle vier door |

De sandboxrun hierboven is van voor deze wijziging, dus de bucket die daar staat heeft nog
geen lock.
