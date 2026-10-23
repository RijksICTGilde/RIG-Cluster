# SeaweedFS Object Storage (proef)

S3-compatibele object storage voor documenten, afbeeldingen en grote bestanden, met
SeaweedFS in plaats van MinIO. Je deployment krijgt een eigen bucket en een eigen
sleutelpaar, en jouw sleutels komen nergens anders bij.

Dit is een **proef**: de dienst staat alleen op de sandbox, zodat we kunnen zien of
SeaweedFS kan wat MinIO bij ons doet. Op productie bestaat hij niet.

## Wanneer gebruik je dit?

- Je wil op de sandbox toetsen of jouw applicatie het met SeaweedFS ook doet.
- Je slaat bestanden op die niet in een database horen: uploads, exports, afbeeldingen.
- Je wil de dienst niet samen met MinIO op dezelfde deployment: ze geven dezelfde
  variabelen, dus kies er een.

## Wat wordt er ingesteld?

- Een bucket per deployment, met de naam `{project}-{deployment}`.
- Een identiteit met rechten op alleen die bucket: lezen, schrijven, verwijderen en
  opsommen. De andere buckets op de server zie je niet.
- Een secret in je pod met dezelfde variabelen als MinIO: `OBJECT_STORE_HOST`,
  `OBJECT_STORE_PORT`, `OBJECT_STORE_USER`, `OBJECT_STORE_PASSWORD`,
  `OBJECT_STORE_BUCKET_NAME`, `OBJECT_STORE_REGION`, plus `OBJECT_STORE_URL` en
  `OBJECT_STORE_ENDPOINT_URL`. Een applicatie wisselt dus alleen van endpoint.
- **Versiebeheer** als je dat aanvinkt: oudere versies van een object blijven bewaard.

## Versiebeheer en oude versies

Zet je **Versiebeheer op de bucket** aan, dan bewaart SeaweedFS oudere versies van een
object als je het overschrijft of verwijdert. Dat kost extra opslag, en daarom heeft elke
bucket met versiebeheer een grens: standaard bewaart hij **hoogstens 30 oude versies per
object**. Je kunt dat aantal per project zetten en per deployment overschrijven, en er een
leeftijdsgrens in dagen bij kiezen. Verder terug dan de grens kan je applicatie dus niet.

Let op, en dit is anders dan bij MinIO: de grens staat als lifecycle-regel op de bucket,
maar SeaweedFS ruimt pas op in zijn eigen opruimronde, en die ronde draait op de sandbox
nog niet vanzelf. De oude versies boven de grens kunnen er dus langer blijven staan dan de
regel zegt.

Wat deze proef nog niet doet:

- **Geen back-up en geen restore.** De back-up-tab biedt deze bucket niet aan.
- **Geen kloon.** Een deployment met `clone-from` kan deze dienst niet gebruiken.
- **Opruimen is direct.** Zet je de dienst uit, dan zijn bucket, identiteit en rechten
  meteen weg, zonder bewaartermijn. Bij MinIO is dat anders.
- **Geen webconsole.** Gebruik de `aws`-CLI tegen `OBJECT_STORE_ENDPOINT_URL`.
- **De opruimronde voor oude versies draait nog niet automatisch** (zie hierboven).
