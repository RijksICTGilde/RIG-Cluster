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

---

## Gebouwd, 7 augustus 2026 (RC-52, PR #53)

### De naamkeuze: `confirm_in_use`

De vlag heet **`confirm_in_use`**, en dat is de vastgelegde beslissing uit punt 4.

De reden staat in de naam zelf. `delete_when_in_use` beschrijft wat er gebeurt maar niet
dat de aanroeper iets bevestigt; `force` zegt alleen dát er iets overruled is, niet wát er
bekend was. De aanroeper bevestigt hier een *feit dat hij in de 409 te horen heeft
gekregen* — dezelfde redenering als bij `rollout=false`. De vlag staat standaard uit: een
vlag die standaard aan staat is gewoon gedrag met een extra naam.

De naam leeft één keer, als `CONFIRM_IN_USE` in
`opi/services/catalog/attachments/api.py`, en de route, de OpenAPI-parameter en het
voorbeeld komen daar allemaal uit.

### Wat er is gebouwd

```
DELETE  /services/attachments/attachments/{attachment_id}[?confirm_in_use=true]
```

- **Niet in gebruik** → weg, zonder bevestiging. 200.
- **In gebruik, geen bevestiging** → 409, met `used_by`: per plek de componentnaam, de
  deployment (als de koppeling daar zit), de soort (`coupling` of `certificate`) en het
  label dat het portaal ook toont.
- **In gebruik, mét bevestiging** → de catalogusregel én elke koppeling gaan weg, in één
  opslag. Blijft er een leeg `attachments`-blok over bij een component, dan gaat dat blok
  weg en blijft alleen de selectie staan. De response meldt in `uncoupled_from` wat er is
  losgekoppeld.
- **Id bestaat niet** → 404.

### Eén afwijking van het plan, bewust

Het plan noemt bij de bevestigde opruiming "componenten en, waar die bestaan,
deployment-componenten". Er is een derde soort verwijzing die het plan niet noemt: een
bijlage die als **publish-on-web-certificaat** dient (`tls: provided`, `attachment: <id>`).

Die wordt **ook mét bevestiging geweigerd.** De reden is precies het uitgangspunt van het
plan — liever weigeren dan half doen. Een koppeling kun je weghalen en dan krijgt het
component simpelweg geen bestand meer. Een certificaatverwijzing kun je dat niet: het
model verwerpt `tls: provided` zonder `attachment`, dus je moet ook beslissen hóe de site
dan wél geserveerd wordt. Een site stilletjes op het platformcertificaat zetten is een
beslissing over publicatie, niet over een bestand dat niemand meer nodig heeft. De 409
zegt daarom wat er eerst moet gebeuren: wijzig de TLS-modus daar.

### De vraag als losse functie

`attachment_usage_sites()` in `opi/handlers/project_file_handler.py` is de ene wandeling
die alle plekken vindt, met een gestructureerde uitkomst (`AttachmentUsageSite`:
component, deployment, soort). `extract_attachment_usage()` is nu de labelprojectie
daarvan. Daardoor kijken de wizard-guard, de bevestigingsmodal, de referentiecontrole en
de API naar dezelfde verzameling plekken — precies wat punt 5 van het plan vroeg.

### Het schema als controle achteraf: geen tweede bevinding

Het plan hield er rekening mee dat de catalogusvalidatie een regel zou missen. Dat is niet
zo: `validate_attachment_references` bestond al, dekt alle verwijzingsplekken en draait bij
elke opslag. Een gemiste opruiming zou daar sneuvelen, en dat is getoetst — inclusief een
toets die bewijst dat de controle tanden heeft (laat één koppeling staan en hij faalt).

### Wat het actiekader ervoor kreeg

- `ActionVerb.DELETE`, met `takes_fields = False`: een delete adresseert één ding via het
  pad en draagt géén body. Een optioneel bestand op een DELETE zou een body zijn die een
  aanroeper kan invullen en die stil genegeerd wordt.
- `ActionFlag`: gedeclareerde booleaanse query-parameters, standaard uit, met een
  omschrijving die in de OpenAPI-parameter landt.
- `verb_examples`: een voorbeeld per werkwoord, want een `curl -X POST -F file=@...` op een
  DELETE-route is erger dan geen voorbeeld — het is een voorbeeld van een ander verzoek.

### De CLI

`zad-cli` leeft in een andere repo (`~/IdeaProjects/zad-cli`) en houdt een kopie van de
spec in `api/upstream-openapi.json`. Hier is dus geleverd wat daar nodig is: de route, de
vlag en de weigering staan volledig in `openapi.json`, inclusief het `used_by`-verhaal en
een `curl -X DELETE`-voorbeeld. Het bijwerken van de CLI zelf hoort in die repo.
