# SeaweedFS-objectopslag

De opvolger van `minio-storage`, met SeaweedFS 4.48 erachter. Sinds RC-382 draagt
`minio-storage` een `replaced_by` naar deze dienst, dus op een cluster waar SeaweedFS er is
(vandaag: de sandbox) ziet een nieuw project de MinIO-kaart niet meer, en houdt een
deployment die MinIO al heeft hem. Zie `features/dienst-vervangen-door-een-opvolger.md`.
Een deployment krijgt een eigen bucket, een eigen identiteit met rechten op alleen die
bucket, en precies dezelfde omgevingsvariabelen als bij MinIO: een applicatie wisselt dus
alleen van endpoint.

MinIO is gearchiveerd en krijgt geen security-fixes meer; de opdrachtgever koos SeaweedFS
als opvolger. RC-331 was de proef die eerst bewees dat SeaweedFS kan wat MinIO bij ons doet,
op de sandbox. Sinds RC-381 draait de dienst op **elk** cluster: `sandboxed-local`,
`odcn-staging` en `odcn-production`. De schakelaar blijft dezelfde: de dienst bestaat op een
cluster waarvan de configuratie een `seaweedfs_host` noemt. Beide diensten staan naast
elkaar; een project kiest er een, en of een nieuw project MinIO nog kan kiezen is een andere
vraag (`features/futures/een-service-kan-vervangen-zijn-door-een-andere.md`).

## Hoe je het gebruikt

Zet `seaweedfs-storage` aan op een project (de kaart staat op de dienstenstap) en
bind hem aan een component. Bij de volgende verwerking maakt OPI de bucket, de identiteit
en de rechten aan, en schrijft een SOPS-secret `{deployment}-seaweedfs` dat als `envFrom`
in de pod komt.

```yaml
services:
  - seaweedfs-storage:
      config:
        enable-versioning: true
```

De pod krijgt dan:

| Variabele | Waarde |
|---|---|
| `OBJECT_STORE_HOST` | `seaweedfs.rig-system.svc.cluster.local` |
| `OBJECT_STORE_PORT` | `8333` |
| `OBJECT_STORE_USER` | `{project}_{deployment}` (de naam IS de access key) |
| `OBJECT_STORE_PASSWORD` | gegenereerd |
| `OBJECT_STORE_BUCKET_NAME` | `{project}-{deployment}` |
| `OBJECT_STORE_REGION` | `us-east-1` |
| `OBJECT_STORE_URL` | `host:port` |
| `OBJECT_STORE_ENDPOINT_URL` | `http://host:port` |

Elke sleutel heeft ook een `APP_`-variant. Dit is dezelfde lijst als bij `minio-storage`
(dezelfde `MinIOVariables`), dus de omschrijvingen noemen nog MinIO; neutraal maken raakt
de MinIO-dienst en de API-uitvoer en hoort bij het productieplan.

Een object met versiebeheer ophalen, met de `aws`-CLI:

```bash
aws --endpoint-url "$OBJECT_STORE_ENDPOINT_URL" --region "$OBJECT_STORE_REGION" \
    s3api list-object-versions --bucket "$OBJECT_STORE_BUCKET_NAME" --prefix rapport.pdf
aws --endpoint-url "$OBJECT_STORE_ENDPOINT_URL" --region "$OBJECT_STORE_REGION" \
    s3api get-object --bucket "$OBJECT_STORE_BUCKET_NAME" --key rapport.pdf \
    --version-id <id> rapport-oud.pdf
```

Path-style adressering en regio `us-east-1`, zoals bij MinIO.

## Configuratie

| Waar | Wat |
|---|---|
| Projectbestand | `enable-versioning` op projectniveau. Niet gezet betekent "niet gekozen" en SeaweedFS laat de bucket dan zoals hij is, net als bij MinIO. |
| `opi/core/cluster_config.py` | `seaweedfs_host` en `seaweedfs_port`, in de blokken `sandboxed-local`, `odcn-staging` en `odcn-production`. Dit is de schakelaar: zonder die sleutels geen kaart, geen opslag, en geen egressregel naar 8333 in de tenant-baseline (`get_ops_egress` leidt die eruit af). |
| `opi/core/config.py` | `SEAWEEDFS_FILER_ADDRESS` en `SEAWEEDFS_MASTER_ADDRESS`, de twee HTTP-adressen die `weed shell` meekrijgt, en `SEAWEEDFS_ADMIN_ACCESS_KEY`/`SEAWEEDFS_ADMIN_SECRET_KEY` voor de bucketkopie van een kloon. Gezet in de OPI-overlay van elk cluster met de dienst. |
| Secret `seaweedfs-credentials` | `WEED_JWT_FILER_SIGNING_KEY` (de admin-Bearer op de IAM-gRPC), `WEED_JWT_SIGNING_KEY` en een admin-sleutelpaar. Gegenereerd door de secret-template, per cluster. Op staging komt het uit de private repo `zad-staging-secrets`. |
| Rotatie van dat secret | Staat in `NON_ROTATABLE` van `scripts/secret_rotate.py`: de admin-identiteit leeft in de filer-configuratie en niet in het bestand, dus een nieuw sleutelpaar in git maakt daar geen identiteit van. Roteren vraagt een eigen procedure langs `weed shell`. |

## Afhankelijkheden

- Het infrastructuurcomponent `infrastructure/bootstrap/infrastructure/seaweedfs/`,
  opgenomen in `clusters/sandboxed-local`, `clusters/odcn` en `clusters/odcn-staging`. Een
  `Deployment` met `weed server`, een PVC (2Gi op de sandbox, 5Gi op staging, 20Gi op
  productie), een `Service` met 8333 (S3), 18888 en 19333 (gRPC) en de metrics-poort, en een
  NetworkPolicy in de **base**: de gRPC-poorten nemen zonder JWT elke aanroep aan, dus de
  afscherming hoort bij het component. Die policy noemt geen namespacenaam in zijn
  selectors (een podSelector zonder namespaceSelector betekent "de eigen namespace"), want
  kustomize herschrijft een selector-waarde niet mee en de namespace heet per cluster
  anders. De overlays van odcn zetten de storageclass, de grootte, het rcr-pad van het
  image, en halen de vaste UID weg omdat OpenShift die uit het bereik van de namespace
  toekent.
- De `weed`-binary in het OPI-image, op dezelfde digest als het component
  (`chrislusf/seaweedfs:4.48@sha256:4e61d15f...`). Die twee gaan samen omhoog. De binary is
  212 MiB, en dat is wat het OPI-image erbij krijgt.
- `mc` in het OPI-image, alleen voor de bucketkopie van een kloon over de S3-poort met het
  admin-sleutelpaar: `weed shell` kent geen bucketkopie. Het beheer van buckets,
  identiteiten en policies loopt nooit die weg op. De `mc`-binary zat al in het image voor
  MinIO.

Een wijziging in dit component komt NIET mee met `sandbox-deploy`: dat rolt alleen OPI uit.
Rol hem uit met `task sandbox:sync` en wacht tot ArgoCD hem heeft toegepast. Meet je rij
G18 of I4 zonder die stap, dan meet je de NetworkPolicy van een vorige ronde.

## Uitrollen op een cluster

Het component en de OPI-configuratie staan in git en gaan via ArgoCD. Twee dingen staan
NIET in git en zijn handwerk per cluster, eenmalig:

1. **Het secret `seaweedfs-credentials`.** De template
   (`infrastructure/bootstrap/infrastructure/secrets/templates/seaweedfs-credentials-secret.yaml`)
   geldt voor elk cluster; `task select-cluster` plus
   `task generate-infrastructure-secrets-for-cluster` rendert hem met de AGE-sleutel van dat
   cluster en zet hem bij in `decrypt-sops.yaml`. Voor staging met `SECRETS_DIR` naar
   `zad-staging-secrets`, want die geheimen staan niet in deze publieke repo. Zonder dit
   secret start de pod niet: de initContainer `eis-admin-identiteit` weigert een leeg
   sleutelpaar, en dat is met opzet (rij G14).
2. **Het pull-secret**, als de namespace het nog niet aan zijn `default`-serviceaccount
   heeft hangen: het image komt via `rcr.rijksapps.nl/dockerhub-rig/`, en dat pad vraagt
   authenticatie. Redis en Prometheus halen hun image langs hetzelfde pad, dus op een
   namespace waar die draaien is er niets te doen.

Daarna syncen `production-infrastructure` (of de staging-tegenhanger) en de
Application van OPI, **in die orde**: de schakelaar van de dienst is `CLUSTER_CONFIG` en
niet of de pod draait, dus een OPI die vooroploopt biedt SeaweedFS aan voordat hij er is.

Reken er ook op dat de **MinIO-kaart op dat cluster verdwijnt voor een nieuw project**:
`minio-storage` draagt sinds RC-382 `replaced_by` naar deze dienst, dus zodra SeaweedFS er
is, is MinIO daar een vervangen dienst. Een project dat hem al heeft houdt hem, met twee
pillen op de kaart; een deployment die hem er nieuw bij zou krijgen wordt geweigerd.

Wat je na de uitrol meet:

| Meting | Hoe |
|---|---|
| De pod draait en is ready | `kubectl -n <ops-ns> get pod -l app=seaweedfs` |
| De initContainer kwam door | dezelfde pod, `Init:0/1` betekent dat het sleutelpaar leeg is |
| Een tenant bereikt 8333 | rij I4 van de eisensuite, of met de hand uit een tenant-pod |
| Een tenant bereikt de gRPC-poorten NIET | dezelfde rij: 18333, 18888 en 19333 horen dicht te zijn |
| De lifecycle-ronde loopt | `kubectl -n <ops-ns> create job lc-proef --from=cronjob/seaweedfs-lifecycle`, en in de log `shards 0-15 complete` |
| OPI bereikt de filer | `s3.bucket.list` via de connector in de OPI-pod (rij G18) |
| Een bucket aan en weer uit | zet de dienst op een testproject aan, verwerk, en zet hem weer uit |
| De scrape loopt | in Prometheus de job `seaweedfs`, en `SeaweedFS_volumeServer_total_disk_size` zodra er data in een bucket staat |

De eisensuite en de e2e van de dienst zijn op een ander cluster te draaien met
`ZAD_OPS_NAMESPACE`, `ZAD_TENANT_NAMESPACE` en `ZAD_E2E_CLUSTER`; zonder die variabelen
meten ze de sandbox.

## De gekozen beheerweg

`weed shell` vanuit de OPI-pod, over de filer-gRPC, met de commando's op **stdin**. Een
wachtwoord in argv is voor iedere andere lezer in de pod te zien in `/proc`. Het nieuwe
IAM-model (`s3.user.create`, `s3.policy -put`, `s3.policy.attach`) neemt hetzelfde
policy-document als de MinIO-policy, dus de isolatie is één document dat beide diensten
delen (`opi/utils/s3_policy.py`).

`weed shell` krijgt altijd beide adressen mee (`-filer` en `-master`): zonder masteradres
blijft hij in `WaitUntilConnected` wachten zonder een fout te geven. Een onbereikbare
master doet hetzelfde, dus elk commando heeft een time-out van 30 seconden
(`SHELL_TIMEOUT_SECONDS`) die daar een leesbare fout van maakt.

Het beleidsdocument is het enige dat niet op stdin past: `s3.policy -put` leest een
bestand. Het gaat daarom als tijdelijk bestand in de OPI-pod (`tempfile` in
`settings.TEMP_DIR`), met een naam per aanroep omdat de taakwerker meerdere gangen
tegelijk in dezelfde pod draait, en het gaat na het commando altijd weer weg.

Afgewezen:

- **De IAM-API op de S3-poort, met boto3 of aiobotocore.** Nieuwe afhankelijkheid, en het
  beheer zou op de poort staan die elke tenant bereikt. Het component zet die API uit
  (`-s3.iam=false`).
- **Het oude actiemodel** (`s3.configure`, `s3.bucket.access`, `Read:<bucket>`). In 4.48
  Hidden, en het zou een tweede vorm van rechten naast het document zijn.
- **`kubectl exec` in de SeaweedFS-pod.** Dan hoeven de interne poorten niet open, maar OPI
  krijgt een `pods/exec`-recht, en wie dat service-account overneemt komt daarmee in elke
  pod van het cluster. Besluit van de opdrachtgever (22-10-2026): voor OPI nooit een optie,
  een extra binary in het image is het kleinere recht. `tests/test_opi_geen_pod_exec.py`
  houdt beide kanten dicht: geen Role of ClusterRole die OPI exec, attach of port-forward
  geeft, en geen exec-aanroep in `opi/`. De meetsuite gebruikt `kubectl exec` wel, want een
  test is geen OPI.

De prijs: de filer- en master-gRPC moeten voor de OPI-pod bereikbaar zijn. Daar staan twee
dingen tegenover: de NetworkPolicy laat die poorten alleen vanuit de OPI-pod toe, en
`jwt.filer_signing.key` maakt dat een aanroep zonder sleutel ook binnen de namespace
geweigerd wordt (nagemeten: `code = Unauthenticated desc = missing authorization metadata`).

## Wat de dienst niet levert

Tot RC-381 stonden back-up, restore, kloon, uitgesteld opruimen en de scrape-job hier als
gat: in de gate gemeten, niet bedraad. Ze zijn nu bedraad. Wat open blijft:

| Niet | Waarom |
|---|---|
| Objectaantal per bucket | SeaweedFS heeft de grootte per bucket wel (`SeaweedFS_volumeServer_total_disk_size{collection=...}`) maar niets dat op `minio_bucket_usage_object_total` lijkt (rij M1). De metrics-explorer noemt het als "niet beschikbaar". |
| Kloon van een remote-source | Een `remote-source` draagt zijn sleutels onder `services: {minio-storage: ...}`, dus dat is een kopie van MinIO naar SeaweedFS: een migratie, met de versiehistorie als open punt. Zie "Wat een migratie van bestaande MinIO-data zou vragen". |
| Bucketgeneraties | MinIO zet een restore in een nieuwe bucket `..._vN`. Deze dienst doet dat niet: een kloon of restore schrijft in dezelfde bucket. |
| De versiehistorie in een kloon | `mc mirror` neemt de laatste versie mee, niet de historie. Hetzelfde gat als bij een migratie. |
| Samen met `minio-storage` op één deployment | Beide diensten geven dezelfde `OBJECT_STORE_*`-variabelen, dus in een pod zou de ene de andere overschrijven. `validate_project` weigert dat. |
| Web-UI | Geen console, geen Admin UI, geen ingress. |
| Quota | Wij gebruiken het bij MinIO ook niet. |

Back-up en restore lopen via het bestaande bucket-pad: beide object-storage-diensten dragen
het `backup_label` **`minio`**, want dat label groepeert diensten met hetzelfde soort
back-up, en dat is hier dezelfde bucket achter dezelfde S3-API. Eén resolver
(`get_object_storage_secret` in `opi/utils/secrets.py`) leest het secret van welke van de
twee een deployment ook gebruikt; de back-uptaak, de back-up-API en de restore-route noemen
de lijst `OBJECT_STORAGE_SERVICE_TYPES`.

Opruimen is `CleanupStrategy.DEFERRED`, met de resourcetypes `seaweedfs_bucket`,
`seaweedfs_user` en `seaweedfs_policy` naast die van MinIO: de naamfuncties zijn voor beide
diensten dezelfde, dus alleen het type zegt waar een bucket staat. De purge staat in
`opi/jobs/reconciliation.py`.

## Wat MinIO-gebruikers missen

**Voor projectteams.** `mc` wordt niet meer gedistribueerd en is tegen SeaweedFS ook niet
nodig. Getoetst in de gate: de `aws`-CLI (rij G13: `ListObjectsV2`, `GetBucketLocation`,
`GetBucketVersioning`, `PutObject` inclusief multipart van 24 MiB, `GetObject` met en
zonder versie, `DeleteObject`, `CopyObject`, een presigned GET) en `mc` zelf, een
minio-go-client (rijen K1, K2, B1 tot B3). **Niet getoetst**: `s5cmd` en `rclone`; die
spreken dezelfde SigV4 met path-style, maar dat is een aanname en geen meting.

**Voor beheer.** `weed shell` in de pod, in plaats van `mc admin`:

| `mc` | `weed shell` | Rij |
|---|---|---|
| `mc ls <alias>` | `s3.bucket.list` (geen JSON, tab-gescheiden regels) | G4 |
| `mc mb` | `s3.bucket.create -name <naam> [-owner <id>]` | G1, G2 |
| `mc rb --force` | `s3.bucket.delete -name <naam>` (ook de versies gaan mee) | G3 |
| `mc version enable` / `suspend` / `info` | `s3.bucket.versioning -name <naam>` met `-enable` of `-suspend` | G5, G6 |
| `mc admin user add` | `s3.user.create -name <id> -access_key <id> -secret_key <sleutel>` | G8 |
| `mc admin user remove` | `s3.user.delete -name <id>` | G10 |
| `mc admin user info` / `list` | `s3.user.show -name <id>` / `s3.user.list` (JSON) | G9 |
| `mc admin policy create` | `s3.policy -put -name <naam> -file <bestand>` | G11 |
| `mc admin policy attach/detach --user` | `s3.policy.attach` / `s3.policy.detach -policy <naam> -user <id>` | G11 |
| `mc admin policy remove` | `s3.policy -delete -name <naam>` | G11 |
| `mc du` | geen tegenhanger. Wel de metriek `SeaweedFS_volumeServer_total_disk_size{collection="<bucket>"}` zodra er data in staat | M1 |

**Geen tegenhanger.** De webconsole: deze dienst heeft geen web-UI. En het **objectaantal**
per bucket: SeaweedFS heeft wel de grootte per bucket, maar niets dat op
`minio_bucket_usage_object_total` lijkt (rij M1).

Wat er **anders** werkt dan bij MinIO:

- `ListBuckets` met de projectsleutels wordt niet geweigerd maar geeft alleen de eigen
  bucket (rij I2). Even goed voor de isolatie, ander antwoord.
- Een tweede `s3.bucket.create` of `s3.user.create` is exit 1 met `already exists` in
  plaats van een stille bevestiging (G1, G8). De connector verdraagt die tekst, dus
  provisioning blijft replay-safe.
- `s3.bucket.list` geeft geen JSON (G4).
- De policy-koppeling verdwijnt met de identiteit: wie een wachtwoord vervangt door de
  gebruiker te verwijderen en opnieuw aan te maken, moet de policy opnieuw koppelen (G10).
  De manager doet dat ook altijd.

## De eisentabel (de gate)

De volledige tabel met per rij de toets en de uitkomst staat in de beschrijving van PR 306
en wordt herhaalbaar gemeten door `tests/e2e/test_sandbox_seaweedfs_eisen.py` (34 rijen, één
test per rij, verslag in `tests/e2e/artifacts/seaweedfs-eisen.md`). Die suite blijft staan
als regressie: een rij die "werkt" meldt wordt rood zodra dat niet meer zo is.

Samengevat: alle rijen in G (gebruik) en I (isolatie) zijn **werkt** of **werkt anders** op
een manier die gebruiker en isolatie niet raakt. K (kloon) en B (back-up) werken ook en zijn sinds
RC-381 bedraad. M1 (metrieken) is het enige echte gat: grootte per bucket wel, objectaantal
niet.

### G20: de grens op oude versies (RC-380, RC-381)

Nieuwe rij, met de hand gemeten op 23-10-2026 tegen SeaweedFS 4.48 op de sandbox (nog niet
in de regressiesuite, want het forceren van een lifecycle-ronde hoort bij die meting):

| Rij | Wat | Uitkomst |
|---|---|---|
| G20 | Een lifecycle-regel met `NoncurrentVersionExpiration` + `NewerNoncurrentVersions` op een bucket met versioning | **werkt**: hij neemt de regel aan, geeft hem terug op `GetBucketLifecycleConfiguration`, bewaart hem als XML in de bucket-entry van de filer, en voert hem uit: 51 versies van een object werden 31 (30 oude plus de actuele) zodra de lifecycle-ronde liep. Die ronde start niet vanzelf; de CronJob `seaweedfs-lifecycle` van het component draait hem eenmaal per nacht (RC-381). |

Wat er precies gemeten is:

- `put-bucket-lifecycle-configuration` met de regel: exit 0. `get-...` geeft hem letterlijk
  terug, en `fs.meta.cat /buckets/<bucket>` laat hem staan onder
  `s3-bucket-lifecycle-configuration-xml`. `ttlSec` blijft 0, ook bij een regel met alleen
  `Expiration.Days`, dus de filer-TTL is hier niet de weg.
- Acht minuten en 24 pollingrondes later stonden er nog 50 versies: er is **geen** vlag in
  `weed server -h` of `weed s3 -h` die een lifecycle-worker start, en de pod logt er niets
  over. 4.48 heeft wel de machinerie: `s3.lifecycle.run-shard` ("manually run one
  daily-replay pass for the given shards", 16 shards) en `s3.bucket.lifecycle.fastpath`.
- `s3.lifecycle.run-shard -shards 0-15 -s3 127.0.0.1:18333 -refresh 0` gaf
  `loaded lifecycle for 1 bucket(s)` en `shards 0-15 complete; cursors checkpointed`, en
  direct daarna waren het 31 versies.

Gevolg voor de dienst: OPI zet de regel (`SeaweedfsConnector.ensure_noncurrent_version_rule`,
over de S3-poort met de admin-sleutels, want `weed shell` heeft geen lifecycle-commando), en
de ronde dwingt hem af. RC-381 bouwde die ronde als CronJob
(`infrastructure/bootstrap/infrastructure/seaweedfs/controller/base/lifecycle-cronjob.yaml`),
zie "De lifecycle-ronde" hieronder. Gemeten met de CronJob erbij: een bucket met versioning,
51 versies van één object, de regel op 30, `kubectl create job
--from=cronjob/seaweedfs-lifecycle`, daarna 31.

### De lifecycle-ronde

Een CronJob in het component en niet in OPI: de ronde is van SeaweedFS en moet ook lopen als
OPI stilstaat. Hetzelfde image op dezelfde digest als de pod, eenmaal per nacht om 03:30
Europe/Amsterdam, `concurrencyPolicy: Forbid`, `activeDeadlineSeconds: 900`, en gehard zoals
de pod: geen serviceaccount-token, `readOnlyRootFilesystem` met `/tmp` als emptyDir,
non-root, alle capabilities gedropt. De odcn-overlays halen de vaste UID weg, zoals bij de
Deployment.

Drie dingen die de aanroep nauw maken, alle drie gemeten op de sandbox:

- **`-refresh 0` is wat er een enkele pass van maakt.** Zonder die vlag blijft het commando
  draaien zonder ooit een pass te doen: tien minuten, 50 versies onveranderd, geen uitvoer.
- **`-filer` en `-master` dragen de HTTP-poort**, 8888 en 9333, niet de gRPC-poort.
  `weed shell` telt er zelf 10000 bij op, zoals `SEAWEEDFS_FILER_ADDRESS` in de configmap
  ook 8888 noemt. Met 18888 dialt hij 28888 en hangt hij zonder foutmelding.
- **De pod heeft meer dan 18333 nodig.** `weed shell` start niet zonder filer en master, dus
  de NetworkPolicy geeft het label `app: seaweedfs-lifecycle` de drie poorten 18333, 18888
  en 19333. Met 18333 dicht deed de ronde zijn werk nog steeds (46 versies werden 31), dus
  die poort staat open voor het adres in de `-s3`-vlag en niet omdat hij een voorwaarde is.
  Een tenant bereikt geen van de drie: gemeten vanuit een clientpod in een tenant-namespace
  is 8333 open en zijn 18333, 18888 en 19333 dicht. De flowmatrix (`tests/netpol/`) legt dat
  vast.

Besluit van 18-10-2026 bij rij G7: de actie `s3:ListBucketVersions` is aan het gedeelde
policy-document toegevoegd. SeaweedFS eist hem voor `ListObjectVersions`; op de
sandbox-MinIO is nagemeten dat die aanroep daar al door `s3:ListBucket` gedekt wordt, dus
MinIO krijgt er niets bij. De actie staat op de bucket-ARN en niet op `*`.

## Wat er op de sandbox gemeten is

De eisentabel (34 rijen) is in zijn geheel groen, en `tests/e2e/test_sandbox_seaweedfs_storage.py`
meet de weg van een klik tot een draaiende pod. Alle **7 metingen zijn groen** (22-10-2026,
313s):

| Meting | Uitkomst |
|---|---|
| De kaart staat op de dienstenstap | werkt |
| De s3-probe van het `e2e-allservices`-image is groen | werkt: schrijven, teruglezen, vergelijken en verwijderen met de sleutels die OPI aanmaakte, via minio-go |
| De DRAAIENDE pod heeft de `OBJECT_STORE_*`-variabelen | werkt: host `seaweedfs.rig-system.svc.cluster.local`, poort 8333, bucket zoals `generate_bucket_name` hem noemt |
| Versiebeheer: de oudste versie byte voor byte terug | werkt |
| Isolatie tussen twee projecten, beide kanten op | werkt: alle zes aanroepen geweigerd, de bucket van B ongewijzigd |
| De dienst uitzetten ruimt alles op | werkt: bucket, identiteit en policy weg, de oude sleutels geweigerd, de bucket van B ongewijzigd |
| Een tenant bereikt 8333 en niet de gRPC-poorten | werkt |

Herhaald op de beheerweg van 22-10-2026 (`weed shell` in de OPI-pod, geen `pods/exec`),
met `sandbox-deploy` plus `task sandbox:sync` voor het component:

| Meting | Uitkomst |
|---|---|
| Stap 9 in zijn geheel | 7 van 7 groen, 416s |
| G18 (connector in de OPI-pod) | werkt: de connector somt `t-a` op, en alleen de pod met `app: operations-manager` komt bij 18888 en 19333 |
| I4 (tenant-poorten) | werkt: alleen 8333 antwoordt; dicht zijn 9333, 8888, 8080, 19333, 18888, 18080, 8181, 9101, 9327 |
| I5 (filer-IAM dicht zonder sleutel) | werkt |
| G1, G8, G11 (de rijen die de connector aanroepen) | werkt |

Een hele rondgang van de connector IN de OPI-pod (aanmaken, `grant_bucket_access`,
versioning, opsommen, ontkoppelen, verwijderen, plus een tweede aanmaak en een tweede
verwijdering voor replay) komt op `buckets: ['rc331-g18']`, `versioning: True`,
`user_exists: True False` en na het opruimen `[]`. De `weed`-binary in dat image is
`version 30GB 4.48 530be3e37 linux amd64`, 222.763.213 bytes.

De gRPC-poorten zijn genoeg: `weed shell` krijgt de HTTP-adressen mee en leidt de
gRPC-poort er zelf uit af, dus de Service en de NetworkPolicy houden 8888 en 9333 dicht.

### Het uitzetten liep eerst stuk op de dienstenmodal

De eerste gang van deze meting faalde, en niet in het opruimpad van deze dienst. De
deselectie landde niet in het projectbestand, dus de reprocess provisioneerde door
(`bucket ... already exists`), en een verwijderpad werd nooit aangeroepen.

De oorzaak, gemeten met een reproductie in de browser tegen de lokale testserver: **een
dienst die als LAATSTE overblijft ging niet uit**, en dat gold net zo voor `minio-storage`.
De kaarten van de dienstenstap zijn vinkjes met de naam `services`, en een vinkje dat uit
staat stuurt niets mee. Is er geen enkele dienst meer aangevinkt, dan draagt de inzending
de sleutel `services` helemaal niet, en `WidgetType.SERVICE_CARDS` viel in de verwerker in
de tak waar een afwezige waarde "ongewijzigd" betekent. `apply_services_mutation` zag
daarna een inzending gelijk aan de basis en had niets te verwijderen.

De reparatie staat in `opi/forms/editables/processor.py`: `SERVICE_CARDS` leest nu als lege
selectie, op dezelfde tak als `CHECKBOX_GROUP`. Dat neemt een asymmetrie weg, want de
dienstenlijst op de componentkaart is een `CHECKBOX_GROUP` en deed dit al. Beide gevallen
(uitvinken naast een andere dienst, en de laatst overgebleven dienst uitvinken) staan nu in
`tests/e2e/test_dienst_met_configstap_uitvinken.py`, lokaal en zonder cluster.

## Kan SeaweedFS MinIO vervangen?

**Ja, voor wat wij van object storage gebruiken.** Alle rijen in G (gebruik) en I
(isolatie) zijn gemeten op de sandbox en komen uit op **werkt** of op **werkt anders** op
een manier die noch de applicatie noch de isolatie raakt. Een applicatie die vandaag op
MinIO draait, wisselt van endpoint en sleutels en merkt verder niets: dezelfde
`OBJECT_STORE_*`-variabelen, dezelfde SigV4 met path-style en regio `us-east-1`, dezelfde
versiebeheer-semantiek, dezelfde weigering op de bucket van een ander.

Dat oordeel geldt voor onze eisen, niet voor MinIO in het algemeen. Wat wij niet gebruiken
is ook niet gemeten: quota, service accounts, lifecycle, object lock, notificaties.

Het gaat over **SeaweedFS als opslag**. De bedrading is een aparte vraag, en die is op de
sandbox in zijn geheel gemeten: zeven van zeven, het uitzetten inbegrepen.

### De gaten, en wat elk gat kost

| Gat | Wat het is | Wat het kost |
|---|---|---|
| Objectaantal per bucket (M1) | SeaweedFS heeft de bucketgrootte wel (`SeaweedFS_volumeServer_total_disk_size{collection=...}`), maar niets dat op `minio_bucket_usage_object_total` lijkt. | `features/storage-metrics-monitoring.md` en de metrics-explorer verliezen een kolom. Zelf tellen vraagt een `ListObjectsV2` per bucket in een job: doenlijk, maar het is werk en het schaalt met het aantal objecten. Het enige echte gat. |
| Geen webconsole | Geen console, geen Admin UI. | Op de sandbox was de MinIO-console een ingress voor beheerders. Beheer gaat nu via `weed shell`; de tabel hierboven geeft per `mc`-handeling de tegenhanger. |
| `s3.bucket.list` geeft geen JSON (G4) | Tab-gescheiden regels. | Al opgelost in de connector; wel een parse die aan de uitvoervorm hangt en dus aan de versie. |
| Een verse filer zonder identiteit staat open (G14) | Zonder ook maar een identiteit zet SeaweedFS `isAuthEnabled` op false. | Het admin-sleutelpaar in `seaweedfs-credentials` is dragend, niet decoratief. Sinds RC-381 staat daar een guard aan twee kanten: een initContainer in de Deployment weigert een leeg sleutelpaar, zodat de pod niet start, en OPI weigert te provisioneren zolang een ongeauthenticeerde `GET /` op de S3-poort met 200 wordt beantwoord (`SeaweedfsConnector.anonymous_access_allowed`). |
| Beheer vraagt open gRPC-poorten (G18) | `weed shell` draait in de OPI-pod en praat over de filer- en master-gRPC, dus die twee poorten staan voor de OPI-pod open. | Het OPI-image draagt de `weed`-binary (212 MiB) mee naar elk cluster. De grens op de poorten is de NetworkPolicy (alleen de pod met `app: operations-manager`) plus de JWT op de filer; de tegenmeting in rij G18 toetst beide. Op odcn haalt de overlay het image via de rcr-proxy, anders blijft de Application eeuwig OutOfSync. |

Rijen K (kloon) en B (back-up) zijn sinds RC-381 bedraad; de kloon van een remote-source
blijft een migratie.

### Wat een migratie van bestaande MinIO-data zou vragen

Niet gebouwd; dit is wat de gate erover zegt. Een kloon van een `remote-source` is
geweigerd omdat die weigering hier op staat.

- **De kopie zelf kan.** K2 is gemeten: `mc mirror` van de sandbox-MinIO naar SeaweedFS en
  terug, met gelijke sha256. Een migratie per bucket is dus een pod met de admin-sleutels
  van beide kanten.
- **Versies zijn het echte werk.** K1 en K2 vergeleken het aantal objecten en de sha256
  van de laatste versie, niet de historie: dat alle oude versies meegaan is in deze gate
  dus **niet gemeten**, en `mc mirror` kopieert naar onze kennis de laatste versie. Een
  bucket met `enable-versioning` aan verliest daarmee zijn oude versies.
  Alle versies meenemen vraagt een eigen gang: `ListObjectVersions` per key, en per versie
  een `GetObject --version-id` plus een `PutObject` in de doelbucket, in volgorde van oud
  naar nieuw, zodat de versievolgorde aan de andere kant overeenkomt. De versie-id's zelf
  gaan niet mee: een applicatie die een versie-id heeft opgeslagen, vindt die na de
  migratie niet terug.
- **Per deployment omzetten.** Beide diensten geven dezelfde variabelen en kunnen niet
  samen op één deployment (`validate_project` weigert dat), dus het omzetten is per
  deployment: kopieren, dienst wisselen, pod herstarten.
- **De MinIO-bucket pas na een bewaartermijn weg**, zodat terugvallen mogelijk blijft.
- **Wat er nog niet is**: een checksumvergelijking per object en per versie als gate voor
  de omzetting, en een plan voor applicaties die versie-id's bewaren.

## Resourcegebruik

Gemeten uit de cgroup van de pod (deze sandbox heeft geen metrics-server, dus `kubectl top`
bestaat er niet):

| Meting | Geheugen | CPU |
|---|---|---|
| Na tien minuten zonder verkeer | 180 MiB | |
| Na een volledige gang van de eisentabel (acht buckets, een object van 24 MiB) | 502 MiB | 15,8 CPU-seconden |
| Na 68 minuten uptime met de hele gate plus een connector-rondgang | 353 MiB | 73,1 CPU-seconden |

Daarom staat het memory-request op **512Mi** en niet op de 256Mi van MinIO; de limit blijft
1Gi. De CPU volgt het sandboxbeleid (request 10m, geen limit) via de patch in
`infrastructure/bootstrap/clusters/sandboxed-local/kustomization.yaml`.

## Verder

- `features/impliciete-dienstselectie.md` - waarom deze dienst zich zelf mag inschrijven
- `instructions/services.md` - het dienstensysteem waar deze dienst in past
- `docs/KNOWN-ISSUES.md` - waarom MinIO een overbrugging is
