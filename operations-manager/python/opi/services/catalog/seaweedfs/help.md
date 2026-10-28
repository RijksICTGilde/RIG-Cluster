# SeaweedFS Object Storage

S3-compatibele object storage voor documenten, afbeeldingen en grote bestanden. Je deployment krijgt een eigen bucket en een eigen sleutelpaar, en jouw sleutels komen nergens anders bij.

## Wanneer gebruik je dit?

- Je slaat bestanden op die niet in een database horen: uploads, exports, afbeeldingen.

## Wat wordt er ingesteld?

- Een identiteit met rechten op alleen die bucket: lezen, schrijven, verwijderen en opsommen. De andere buckets op de server zie je niet.
- Een secret in je pod met de variabelen: `OBJECT_STORE_HOST`, `OBJECT_STORE_PORT`, `OBJECT_STORE_USER`, `OBJECT_STORE_PASSWORD`, `OBJECT_STORE_BUCKET_NAME`, `OBJECT_STORE_REGION`, plus `OBJECT_STORE_URL` en `OBJECT_STORE_ENDPOINT_URL`.
- **Versiebeheer** als je dat aanvinkt: oudere versies van een object blijven bewaard.
- **Back-up en restore**, op de back-up-tab.
- **Kloon** van de bucket van een andere deployment of uit een back-up.
- **Uitgesteld opruimen.** Zet je de dienst uit, dan gaan bucket, identiteit en rechten pas na de bewaartermijn weg, zodat terugzetten mogelijk blijft.

## Versiebeheer en oude versies

Zet je **Versiebeheer op de bucket** aan, dan bewaart SeaweedFS oudere versies van een object als je het overschrijft of verwijdert. Dat kost extra opslag, en daarom heeft elke bucket met versiebeheer een grens: standaard bewaart hij **hoogstens 30 oude versies per object**. Je kunt dat aantal per project zetten en per deployment overschrijven, en er een leeftijdsgrens in dagen bij kiezen.
