# Oude versies in een bucket hebben een grens

Wat het is: elke bucket met versioning die OPI beheert, in MinIO en in SeaweedFS, draagt een lifecycle-regel
die oude versies boven een grens opruimt. De grens is standaard **30 oude versies per
object**, en een project kan hem zelf kiezen, voor het hele project of per deployment.
Een leeftijdsgrens in dagen kan erbij.

Waarom dat nodig was: versioning aanzetten was mogelijk (`enable-versioning: true`), maar
er stond nergens een `mc ilm`-regel. Een bucket met versioning hield dus **elke** oude
versie van **elk** object, onbeperkt. Een applicatie die per bewerking een versie
wegschrijft (Docs van La Suite doet dat voor zijn documentgeschiedenis) laat zo'n bucket
zonder grens groeien, en niemand merkt dat tot de opslag vol is.

## Wat een project kan kiezen

| veld | lagen | standaard | ondergrens | bovengrens |
|---|---|---|---|---|
| `max-noncurrent-versions` | project, deployment | 30 | 1 | 1000 |
| `noncurrent-expire-days` | project, deployment | geen grens | 1 | 3650 |

Specifieker wint: een deployment boven het project, het project boven de standaard. Een
bucket hoort bij een deployment, dus de deploymentwaarde is wat op de bucket komt. Een
ontbrekend veld betekent de minder specifieke laag, en uiteindelijk de standaard, dus een
migratie van bestaande projectbestanden is niet nodig.

```yaml
services:
  - name: minio-storage # of seaweedfs-storage, met dezelfde velden
    config:
      enable-versioning: true
      max-noncurrent-versions: 50
      noncurrent-expire-days: 90

deployments:
  - name: productie
    services:
      - reference: minio-storage
        config:
          max-noncurrent-versions: 200
```

Dit loopt via de speelruimte van een dienst (`speelruimte-van-een-dienst.md`), dus de
grenzen staan op één plek: in de declaratie in
`opi/services/catalog/shared/noncurrent_versions.py`. Het wizardveld, de API-documentatie
en de weigering bij het opslaan lezen daar alle drie uit. De declaratie staat in `shared/`
en niet in een dienstpakket, want `minio-storage` en `seaweedfs-storage` dragen dezelfde
grens: één declaratie, en elke dienst geeft zijn eigen `ServiceType` mee bij het oplossen,
zodat het yaml-pad zijn eigen dienst noemt.

### Geen standaard voor de leeftijdsgrens

`noncurrent-expire-days` heeft geen standaardwaarde: een leeftijdsgrens is een keuze, en in
een reeks die bij 1 dag begint is er geen getal dat "geen grens" betekent. Daarvoor is de
declaratie uitgebreid in plaats van er een tweede soort naast te zetten: `default=None`
betekent "niet gezet", `resolve_setting` geeft dan None, en de regel laat
`NoncurrentDays` weg. 0 als magische waarde zou in een bereik vanaf 1 niets betekenen.

## Op het scherm

- **Projectniveau**: in "Objectopslag configuratie", onder het vinkje voor versiebeheer, een
  veldgroep "Oude versies" met het aantal en de leeftijd. De keuzelijst biedt stappen aan
  (5, 10, 20, 30, 50, 100, 250, 1000 en 7, 30, 90, 180, 365, 730, 3650); een opgeslagen
  waarde buiten die stappen blijft zichtbaar als eigen waarde.
- **Per deployment**: de knop "Oude versies" op de deploymentkaart opent
  `modal-edit-minio-deployment-<index>`. De lege keuze heet daar "Volg het project (N)".

Voor `seaweedfs-storage` staan dezelfde velden in "SeaweedFS-objectopslag configuratie" en
dezelfde knop op de deploymentkaart (`modal-edit-seaweedfs-deployment-<index>`).

De knop staat er zodra het project `minio-storage` gebruikt, ook als het projectbestand
niets over versioning zegt. Of de bucket echt versioning heeft is niet uit het projectbestand
te weten: het kan buiten OPI om aangezet zijn. De helptekst zegt daarom dat de grens alleen
geldt voor een bucket met versiebeheer.

## De regel op de bucket

Na het zetten van versioning legt `MinioManager._apply_noncurrent_version_limit` de regel
neer, op **elke** bucket die versioning heeft, ook een bucket waar het buiten OPI om
aanstaat. Een bucket zonder versioning heeft geen oude versies en krijgt dus geen regel;
versioning zelf wordt nooit aangezet op grond van deze instelling.

De regel zelf:

```json
{
  "ID": "zad-noncurrent-versions",
  "Status": "Enabled",
  "Expiration": {"ExpiredObjectDeleteMarker": true},
  "NoncurrentVersionExpiration": {"NewerNoncurrentVersions": 30, "NoncurrentDays": 90}
}
```

`NoncurrentDays` staat er alleen als er een leeftijdsgrens is. `ExpiredObjectDeleteMarker`
ruimt een delete marker op waar geen versie meer achter staat, wat precies is wat
overblijft als de versies eronder zijn verlopen.

`MinioConnector.ensure_noncurrent_version_rule` is idempotent en **raakt andere regels
niet**: hij exporteert de lifecycle (`mc ilm rule export`), vervangt alleen de regel met
dit ID, laat de rest staan en importeert het document terug (`mc ilm rule import`). Een
gelijke regel opnieuw zetten schrijft niets. Een bucket zonder lifecycle-configuratie laat
`mc` falen op de export; dat is hier geen fout maar "nog geen regels".

MinIO voert ILM asynchroon uit, in een scanronde. De regel staat dus meteen op de bucket,
maar de oude versies verdwijnen pas als de scanner langs is geweest.

Bij `seaweedfs-storage` doet `SeaweedfsManager._grens_op_oude_versies` hetzelfde, met
`get_versioning` als poort en hetzelfde document; zie "SeaweedFS" hieronder voor de twee
verschillen.

## Bestaande buckets

Bij de eerstvolgende verwerking van een project krijgt elke bucket met versioning de regel.
Op productie (gemeten 23-10-2026 in de projectbestanden) gebruiken 11 projecten
`minio-storage` en zet er één `enable-versioning: true` (`mb-grist-helmfile`). Een bucket
waar versioning buiten OPI om aanstaat valt er ook onder; `mb-docs-helmfile` is de
verwachte tweede. Bij de eerste ILM-ronde verdwijnt daar alles boven de 30 nieuwste oude
versies per object. Dat is onomkeerbaar, dus voor die twee buckets hoort er een handmatige
meting bij de uitrol (`mc version info` en `mc du --versions`, vóór en een dag erna).

## Wat dit voor herstellen betekent

Een herstel naar een eerdere versie van een object doet de **applicatie**, met de S3-API op
haar eigen bucket. Daar geldt de grens: verder terug dan 30 oude versies (of dan de gekozen
waarde, en met een leeftijdsgrens niet langer terug dan die dagen) is er niets meer om naar
terug te gaan.

De backup- en herstelroute van OPI zelf staat hier los van: die werkt per **generatie** van
de bucket (`restore_router.py`, `generate_bucket_name`) en kopieert met `mc mirror` of Kopia
alleen de huidige versie van elk object. Een backup bevat dus geen versiegeschiedenis, en
een herstel leunt niet op oude versies. De versiegeschiedenis zit alleen in de bucket zelf.

## SeaweedFS

`seaweedfs-storage` (RC-331) draagt dezelfde grens, uit dezelfde declaratie, met dezelfde
knop op de deploymentkaart (`modal-edit-seaweedfs-deployment-<index>`).

Gemeten op de sandbox (SeaweedFS 4.48, 23-10-2026): hij **aanvaardt en voert de regel uit**.
Een bucket met 50 versies van één object zakte naar 31 (30 oude plus de actuele) zodra zijn
lifecycle-ronde liep. De uitkomst staat als rij G20 in de eisentabel van
`seaweedfs-objectopslag.md`.

Twee dingen zijn anders dan bij MinIO:

- **De weg naar de regel.** `weed shell` heeft geen lifecycle-commando (4.48 kent alleen
  `s3.bucket.lifecycle.fastpath` en `s3.lifecycle.run-shard`), dus
  `SeaweedfsConnector.ensure_noncurrent_version_rule` gaat over de S3-poort met het
  admin-sleutelpaar uit `seaweedfs-credentials`. De S3-client van dit project is `mc` in
  `MinioConnector`, en het regel-document en zijn idempotentie staan daar al, dus die
  worden hergebruikt onder de alias `default-seaweedfs`.
- **Wanneer hij uitvoert.** SeaweedFS ruimt op in een "daily replay"-ronde per shard, en die
  ronde draait in deze uitrol **niet vanzelf**: er is geen vlag in `weed server`/`weed s3`
  die hem start en er staat geen worker in de pod. Acht minuten na het zetten van de regel
  was er niets opgeruimd; na `s3.lifecycle.run-shard -shards 0-15 -s3 127.0.0.1:18333` was
  het in één keer 31. De grens staat dus wel op de bucket, maar wordt pas afgedwongen als
  die ronde loopt. Een periodieke trigger (een CronJob in het seaweedfs-component die
  `weed shell s3.lifecycle.run-shard -shards 0-15 -s3 <s3>:18333` draait, of één
  langdraaiende pass met `-refresh 24h`) is wat dat zou kosten; die is NIET gebouwd, want
  deze taak zet de regel en SeaweedFS is een proef op de sandbox.

## Afhankelijkheden

- `opi/services/catalog/shared/noncurrent_versions.py` - de declaratie, de samenvoeging en
  de sectie van de deploymentmodal
- `opi/services/catalog/minio/visualizers.py`, `actions.py` en dezelfde twee in
  `catalog/seaweedfs/` - de velden en de knop per dienst
- `opi/connectors/minio_mc.py` - `get_bucket_lifecycle`, `ensure_noncurrent_version_rule`
- `opi/connectors/seaweedfs_shell.py` - `ensure_noncurrent_version_rule` over de S3-poort
- `opi/manager/minio_manager.py` - `_apply_noncurrent_version_limit`
- `opi/manager/seaweedfs_manager.py` - `_grens_op_oude_versies`
- `features/speelruimte-van-een-dienst.md` - het mechanisme eronder
