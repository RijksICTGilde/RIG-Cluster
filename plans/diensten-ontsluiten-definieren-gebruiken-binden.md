# Diensten ontsluiten: definiëren, gebruiken, binden

Status: plan, 6 augustus 2026. Niet gebouwd. Aanleiding: de service-API toont voor attachments alleen de component-koppeling, de velden hebben geen uitleg, en de inhoud van een bijlage kan er niet eens in.

## Wat er nu is, gemeten

Attachments draagt een `config_model` op **alle vier** de lagen. Er bestaat precies **één** route:

```
PUT/DELETE /api/v2/projects/{project}/services/attachments/config/component/{component}
```

De projectlaag heeft dus wel een model maar geen route. De reden staat in de routegenerator: die hangt aan de aanwezigheid van **editables**, en attachments heeft er op projectniveau nul. De API is daarmee afgeleid uit wat de wizard toevallig als veld toont, niet uit wat de dienst kan.

Verder:

- `AttachmentUse` heeft **0 van de 4** velden met een omschrijving, `AttachmentsConfig` 0 van 1.
- De inhoud van een bijlage komt alleen binnen via wizardroutes (`/forms/wizard/{flow_id}/attachments/stage`), sessie- en CSRF-gebonden. Een API-client kan dus een **verwijzing** leggen naar iets dat hij niet kan **maken**.
- Er is **geen enkele groottelimiet** op een bijlage, niet in de route, niet in de dienst, niet in het model.

## De drie soorten config

"Service-config" is nu één begrip terwijl het er drie zijn. Bij attachments zijn ze alle drie zichtbaar, en de data kent ze al:

| Soort | Waar het vandaag staat | Voorbeeld |
|---|---|---|
| **Definiëren** | de projectcatalogus, onder `data` | een bestand met `id`, `filename`, `content` |
| **Gebruiken** | het component, onder `config` | `reference` naar dat id |
| **Binden** | idem, de koppeling zelf | `provide-as`, `path`, `env-name` |

Definiëren zet iets in het systeem zonder dat het gebruikt wordt. Het schema zegt zelf dat die kant nergens thuishoort:

> *"NOT referenced from anywhere ... So this shape is currently validated by nothing."*

"Ik wil dienst X gebruiken op dit component" komt vaker terug dan alleen bij attachments; bij de meeste diensten is het vandaag impliciet omdat er niets te definiëren valt. Attachments is de eerste waar de drie uit elkaar lopen, en daarmee de plek om het begrip goed neer te zetten.

## Wat een gebruiker moet kunnen

1. **Op projectniveau bijlagen uploaden**, elk met bestand, pad en identifier.
2. **Bij een component zeggen: gebruik bijlage met id X**, met de bindingsdetails erbij.
3. **Het ook in één keer op een component doen**, dus uploaden en binden tegelijk.
4. **Upserten op beide niveaus**: bestaat het id al, dan vervangen.

## De werkwoorden, en waarom ze uit elkaar blijven

Beslist op 6 augustus, en dit is geen detail maar het contract:

| Actie | Betekenis | Bestaat het id al |
|---|---|---|
| `POST` | toevoegen | **weigeren** (409) |
| `PUT` / `PATCH` | bijwerken | mag |
| upsert | vervangen, expliciet als zodanig aangeroepen | vervangt zonder vragen |

Vervangen gebeurt **op id, zonder waarschuwing**. Of dat de bedoeling was, is aan de aanroeper. Juist daarom staat dat gedrag alleen op de upsert: een `POST` die stilletjes overschrijft liegt over wat hij doet. Een bevestigingsstap kan later, als blijkt dat mensen zich eraan branden.

## Voorstel

1. **Benoem de drie soorten in het servicecontract**, per laag: definieert deze dienst hier iets, gebruikt hij iets, of bindt hij iets.
2. **Editables blijven het uitgangspunt**, want dat werkt voor de meeste diensten. Maar een dienst mag, en soms moet, **extra acties expliciet declareren**. Die declaratie hoort bij de dienst, desnoods in een eigen `api.py` in zijn map, in lijn met RC-36: alles van een dienst op één plek.
3. **Een API-veld hergebruikt zijn editable.** De validatieregels staan daar, en die horen nergens een tweede keer te bestaan. Dat patroon is er al sinds RC-26: `opi/api/validation.py` bouwt nul eigen `Editable`s meer en verwijst naar de gedeelde (`COMPONENT_IMAGE_EDITABLE`, `WIZARD_DEPLOYMENT_NAME_EDITABLE`) met alleen een `_required` of `_optional` eromheen. Volg dat. Een veld dat geen editable kent is een bewuste uitzondering die je opschrijft, geen tweede validator.
4. **Declareer slim, schrijf niet uit.** Een actie zegt: welke velden, wat betekent elk veld (**ook in de OpenAPI-spec**), een voorbeeld van gebruik, en welke veldcombinaties geldig zijn per werkwoord. Daaruit volgen route, model en documentatie, in plaats van drie keer hetzelfde met de hand.
5. **De attachment-catalogus modelleren**, zodat de definieer-kant dezelfde behandeling krijgt als de rest en niet langer door niets gevalideerd wordt.
6. **Upload-endpoint met multipart**, projectniveau en componentniveau, met een grens van **64 KB**. Bijlagen zijn bedoeld voor kleine bestanden zoals certificaten. Die grens bestaat vandaag niet en moet dus gebouwd worden.
7. **Veldomschrijvingen verplicht** op de configmodellen, met een test die faalt als een veld er geen heeft.

## Volgorde

1. De drie soorten benoemen en attachments erop uitdrukken, zonder dat er een route verandert. Dat is de ontwerpstap.
2. De catalogus modelleren en laten valideren. Verifiëren: bestaande projectbestanden met bijlagen valideren nog steeds, en een kapotte catalogus wordt nu geweigerd in plaats van genegeerd.
3. De actie-declaratie, met attachments als eerste bewoner: de werkwoorden, de veldcombinaties, de uitleg en het voorbeeld.
4. Het upload-endpoint met de 64 KB-grens, eerst op projectniveau, daarna de component-in-één-keer die intern hetzelfde doet plus de binding.
5. Routegeneratie uit de lagen, en de veldomschrijvingen met hun test.

## Waar op te letten

**Editables en API zullen nooit precies samenvallen, en dat hoeft ook niet.** Het doel is niet één bron voor allebei maar dat ze dicht bij elkaar liggen en allebei bij de dienst horen. Forceer geen gelijkheid die er niet is; zorg dat een verschil zichtbaar en bedoeld is.

**Niet elke laag hoort een route te krijgen.** "Config op een laag, dus een endpoint" is de goede richting, maar controleer per dienst of het betekenis heeft. Een endpoint dat niets zinnigs doet is slechter dan geen endpoint.

**De 64 KB-grens is een keuze, geen natuurwet.** Hij hoort op één plek te staan, met de reden erbij, zodat verhogen een besluit is en geen ontdekking. En hij moet gelden voor élke weg naar binnen, dus ook de wizard, anders verplaatst het probleem zich.

**Het projectbestand groeit hiervan.** Dat is bekend en bewust uitgesteld (`project_attachments_yaml_size`): als het knelt gaan we naar YAML-imports met elke bijlage in een eigen bestand. Niet nu oplossen, wel weten dat het de reden is dat de grens laag staat.

**Dit raakt meer diensten dan attachments.** Bouw het contract zo dat de volgende dienst met een definieer-kant er niets voor hoeft te verzinnen.
