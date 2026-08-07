# Een bijlage kan ook weer weg

Status: plan, 7 augustus 2026. Niet gebouwd. Aanleiding: je kunt bijlagen uploaden en bijwerken, maar niet verwijderen. Ze stapelen zich op in het projectbestand en er is geen weg terug.

## Wat er nu is, gemeten

Zes routes, en geen ervan verwijdert een bijlage:

```
POST    /services/attachments/attachments                                   uploaden (project)
PUT     /services/attachments/attachments/{attachment_id}                   bijwerken (project)
POST    /services/attachments/component/{component_name}/attachments        uploaden + koppelen
PUT     /services/attachments/component/{component_name}/attachments/{id}   bijwerken + koppelen
PUT     /services/attachments/config/component/{component_name}             koppeling zetten
DELETE  /services/attachments/config/component/{component_name}             koppeling weghalen
```

**Die DELETE ontkoppelt, hij verwijdert niet.** De omschrijving is eerlijk: *"Remove the `attachments` config block from the `services` list of component"*. Dat is het juiste gedrag, want een bijlage kan door meer dan een component gebruikt worden. Maar het betekent ook dat de bijlage zelf in de catalogus achterblijft, en niets haalt hem daar ooit weg. Ook de wizard niet.

**De twee kanten staan los van elkaar in de data.** De catalogus staat op projectniveau onder `data` (`id`, `filename`, `content`), en een component verwijst ernaar met `reference`. Verwijderen raakt dus twee plekken, en dat is precies waarom het zorgvuldig moet.

## Wat het moet worden

Een `DELETE` op de bijlage zelf, op id. Met dit gedrag:

1. **Wordt hij nergens gebruikt, dan gewoon weg.** Dat is het normale geval en vraagt geen bevestiging.
2. **Wordt hij wel gebruikt, dan weigeren.** Met in het antwoord *waar* hij gebruikt wordt, zodat de aanroeper weet wat hij op het spel zet. Niet stil verwijderen en de verwijzingen laten breken.
3. **Tenzij er een bevestiging meekomt.** Dan gaat de bijlage weg en worden de plekken die ernaar verwijzen mee aangepast: componenten en, waar die bestaan, deployment-componenten.

Dat is de vorm die de gebruiker vroeg: normaal is verwijderen goedkoop, en alleen als het gevolgen heeft moet je zeggen dat je die gevolgen accepteert.

## Voorstel

1. **`DELETE /services/attachments/attachments/{attachment_id}`**, naast de bestaande `PUT` op datzelfde pad. Dat leest vanzelf: dezelfde bron, andere werkwoorden.

2. **Zonder bevestiging is "in gebruik" een weigering, geen fout in de aanroep.** Antwoord met de lijst van gebruikers (componentnaam, en de deployment als het daar zit), zodat een client of de CLI die kan tonen. Een 409 past hier: de toestand verhindert het, niet het verzoek.

3. **Met bevestiging wordt het een opruiming in één keer.** De catalogusregel weg, en elke verwijzing eruit. Blijft er bij een component een leeg `attachments`-blok over, dan gaat dat blok ook weg, want dat is wat de bestaande `clear` ook doet.

4. **De naam van de vlag is een beslissing, geen detail.** "delete when in use" beschrijft wat er gebeurt maar niet dat je iets bevestigt. Iets als `confirm_in_use=true` zegt beide: je weet dat hij in gebruik is en je wilt het toch. Leg de keuze vast met de reden, zoals bij `rollout=false`.

5. **Eén weg, ook voor de wizard.** Als de wizard later een verwijderknop krijgt, hoort die langs dezelfde regel te lopen. Bouw de "wordt hij gebruikt"-vraag dus als iets dat beide kanten kunnen stellen, niet als iets dat in de route zit.

## Volgorde

1. De vraag "waar wordt deze bijlage gebruikt" als losse functie, met een test op alle plekken waar een verwijzing kan staan (component en deployment-component). Dat is het fundament van de rest.
2. De `DELETE` zonder bevestiging: verwijdert als hij vrij is, weigert met de gebruikers erbij als hij dat niet is.
3. De bevestigde variant, die de verwijzingen mee opruimt. Verifieerbaar: na afloop valideert het projectbestand nog, en er staat nergens een verwijzing naar een id dat niet meer bestaat.
4. De naamkeuze vastleggen in de plan- en featuredocumentatie.

## Waar op te letten

**Een verwijzing die blijft hangen is erger dan een bijlage die blijft staan.** Vandaag stapelen bijlagen zich op, en dat is rommelig maar veilig. Een halve verwijdering breekt het projectbestand. Dus liever weigeren dan half doen, en de bevestigde variant in één transactie.

**Het schema is de controle achteraf.** Na een bevestigde verwijdering hoort het projectbestand nog door `validate_project_schema` te komen, en een verwijzing naar een verdwenen id hoort daar te sneuvelen. Als dat niet zo is, mist de catalogusvalidatie een regel en is dat een tweede bevinding.

**Dit raakt de CLI.** `zad-cli` heeft de attachment-endpoints nog niet, dus daar hoort dit meteen goed in te landen in plaats van later te worden bijgeplakt.

**Niet uitbreiden naar meer dan bijlagen.** De verleiding is om hier een algemeen "wordt dit ergens gebruikt"-mechanisme van te maken voor alle diensten. Dat kan later; nu is er een concreet gat met een concreet gedrag, en dat is genoeg.
