# Permanente opslag

Een schijf die aan je component gekoppeld wordt en waarvan de inhoud bewaard blijft. Herstart de pod, of wordt hij vervangen bij een nieuwe uitrol, dan staan je bestanden er nog steeds.

## Wanneer gebruik je dit?

- Je applicatie schrijft bestanden weg die niet verloren mogen gaan
- Je gebruikt een ingebouwde database of zoekindex die op schijf staat
- Je wilt uploads bewaren en gebruikt geen MinIO

## Verschil met tijdelijke schijfruimte

**Permanente opslag** overleeft een herstart, **Tijdelijke schijfruimte** niet. Permanente opslag kost blijvend ruimte en kan meegenomen worden in een backup. Gebruik tijdelijke schijfruimte voor werkbestanden en caches die je zo weer kunt aanmaken; kies permanente opslag alleen als verlies van de gegevens een probleem is.

## Wat wordt er ingesteld?

Er wordt een volume (een PVC) aangemaakt en in je pod gekoppeld. Per volume geef je een naam, een grootte en een koppelpad op; standaard is dat 100Mi op **/data**. Je component krijgt de variabele **DATA_PATH**.

Je kunt kiezen uit 50Mi, 100Mi, 250Mi, 500Mi en 1Gi; 1Gi is het maximum per volume. Begin klein: een volume kan wel groeien en niet krimpen.

Bij het verwijderen van deze service wordt de opslag niet meteen weggegooid, maar gemarkeerd voor uitgestelde verwijdering, zodat een vergissing te herstellen is.
