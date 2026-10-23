# MinIO Object Storage

Opslag voor bestanden via een S3-compatibele koppeling. Je krijgt een eigen bucket waarin je applicatie documenten, afbeeldingen of grote bestanden kan zetten en weer ophalen.

## Vervangen door SeaweedFS

Op een cluster waar **SeaweedFS Object Storage** beschikbaar is, is deze dienst zijn
opvolger: MinIO distribueert zijn images niet meer, en SeaweedFS kan wat wij van MinIO
gebruiken. Daar geldt:

- een nieuw project of een nieuwe deployment kiest SeaweedFS; de MinIO-kaart staat er niet
  meer, en een projectbestand of API-aanroep die hem toch kiest wordt geweigerd;
- een deployment die MinIO al heeft, houdt hem: bucket, back-up, restore en configuratie
  blijven werken, en de kaart blijft staan zodat je hem kunt bewerken;
- zet je hem uit, dan kun je hem niet opnieuw aanzetten.

Op een cluster zonder SeaweedFS (vandaag: productie) verandert er niets. Zie
`features/dienst-vervangen-door-een-opvolger.md` en `features/seaweedfs-objectopslag.md`.
De migratie van bestaande buckets komt apart.

## Wanneer gebruik je dit?

- Gebruikers uploaden bestanden die je moet bewaren
- Je bewaart grote bestanden die niet in een database horen
- Je hebt meerdere replica's die bij dezelfde bestanden moeten kunnen
- Je gebruikt al een S3-bibliotheek in je applicatie

Voor bestanden die bij één pod horen en niet gedeeld hoeven te worden is **Permanente opslag** eenvoudiger.

## Wat wordt er ingesteld?

Er worden een bucket en toegangssleutels aangemaakt. Je component krijgt **OBJECT_STORE_HOST**, **OBJECT_STORE_PORT**, **OBJECT_STORE_USER**, **OBJECT_STORE_PASSWORD**, **OBJECT_STORE_BUCKET_NAME** en **OBJECT_STORE_REGION**. Bij het verwijderen van de service wordt de bucket gemarkeerd voor uitgestelde verwijdering, zodat je bestanden niet meteen weg zijn.

## Eén bucket per deployment

De bucket en toegangssleutels horen bij een deployment. Heeft je deployment meerdere componenten die deze service gebruiken, dan delen ze allemaal dezelfde bucket en dezelfde toegangssleutels.
