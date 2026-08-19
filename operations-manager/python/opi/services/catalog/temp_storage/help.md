# Tijdelijke schijfruimte

Werkruimte op schijf die bij je pod hoort en samen met die pod verdwijnt. Handig als je applicatie ruimte nodig heeft om iets weg te schrijven, maar de inhoud niet hoeft te bewaren.

## Wanneer gebruik je dit?

- Je verwerkt bestanden die je daarna weer weggooit, zoals uploads of exports
- Je hebt een cache of scratch-ruimte nodig
- Je applicatie schrijft tijdelijke bestanden groter dan het containergeheugen

## Verschil met permanente opslag

Bij een herstart of een nieuwe uitrol is deze schijf leeg. Moet de inhoud een herstart overleven, kies dan **Permanente opslag**. Twijfel je: begin hier, want tijdelijke ruimte kost niets blijvend en dwingt je applicatie niet aan een schijf vast te zitten.

## Wat wordt er ingesteld?

Er wordt een vluchtig volume in je pod gekoppeld. Per volume geef je een naam, een grootte en een koppelpad op; standaard is dat 100Mi op **/tmp**. Je component krijgt de variabele **TEMP_PATH**.

Je kunt kiezen uit 50Mi, 100Mi, 250Mi, 500Mi en 1Gi; 1Gi is het maximum per volume. Schrijft je applicatie meer weg dan de opgegeven maat, dan wordt de pod herstart, dus geef op wat je werkelijk nodig hebt.
