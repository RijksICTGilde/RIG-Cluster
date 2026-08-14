# Bijlagen

Met bijlagen koppel je een bestand aan een component, bijvoorbeeld een certificaat, een keystore of een CA-bundel. Je uploadt het bestand in het portaal; het wordt versleuteld bij je project bewaard en bij het uitrollen in je pod gezet.

## Wanneer gebruik je dit?

- Je applicatie heeft een certificaat of sleutelbestand nodig
- Je moet een CA-bundel meegeven om een andere partij te vertrouwen
- Je wilt je eigen certificaat op je webadres aanbieden
- Je hebt een configuratiebestand dat niet in je container-image thuishoort

## Wat wordt er ingesteld?

Je geeft elke bijlage een identifier en kiest per component hoe hij wordt aangeleverd: als **bestand** op een pad in de pod, of als **omgevingsvariabele** met de inhoud als waarde (alleen voor tekstbestanden). Een bijlage kan aan meerdere componenten gekoppeld worden, en je kunt per deployment afwijken.

Een bestand is maximaal 64 KB. De inhoud wordt versleuteld opgeslagen; er is geen database of aparte opslag voor nodig.

## Een bijlage vervangen

Loopt een certificaat af, gebruik dan **Vervangen** bij de bijlage en niet verwijderen-en-opnieuw-uploaden. Bij vervangen blijft de identifier staan, en daarmee blijven alle koppelingen staan: elk component dat de bijlage gebruikt, blijft eraan gekoppeld. Verwijderen verbreekt die koppelingen.

De identifier ligt bij een vervanging vast; alleen de inhoud gaat eroverheen. De naam van het nieuwe bestand wordt overgenomen. De vorige inhoud is daarna weg -- er is geen versiehistorie. Via de API is dit `PUT` op de bijlage zelf, die een id die niet bestaat weigert.
