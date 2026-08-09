# MinIO Object Storage

Opslag voor bestanden via een S3-compatibele koppeling. Je krijgt een eigen bucket waarin je applicatie documenten, afbeeldingen of grote bestanden kan zetten en weer ophalen.

## Wanneer gebruik je dit?

- Gebruikers uploaden bestanden die je moet bewaren
- Je bewaart grote bestanden die niet in een database horen
- Je hebt meerdere replica's die bij dezelfde bestanden moeten kunnen
- Je gebruikt al een S3-bibliotheek in je applicatie

Voor bestanden die bij één pod horen en niet gedeeld hoeven te worden is **Permanente opslag** eenvoudiger.

## Wat wordt er ingesteld?

Er worden een bucket en toegangssleutels aangemaakt. Je component krijgt **OBJECT_STORE_HOST**, **OBJECT_STORE_PORT**, **OBJECT_STORE_USER**, **OBJECT_STORE_PASSWORD**, **OBJECT_STORE_BUCKET_NAME** en **OBJECT_STORE_REGION**. Bij het verwijderen van de service wordt de bucket gemarkeerd voor uitgestelde verwijdering, zodat je bestanden niet meteen weg zijn.
