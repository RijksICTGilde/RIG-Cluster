# Een omschrijving op een component

Een component krijgt een optioneel veld `description`: een vrije tekst die zegt wat het component is en doet ("API voor de zoekfunctie, leest uit de gedeelde database"). Het project heeft zo'n veld al (`description` op de root); een component nog niet. Wie een project opent ziet nu alleen namen en een type, en moet raden wat `worker-2` doet.

Alle nieuwe namen hieronder zijn een VOORSTEL, tenzij er een codeanker bij staat. Wijk je af, zeg het in de PR.

## Wat er nu staat

- **Schema**: `$defs`/`components.items` in `operations-manager/python/opi/schemas/project_v2.json` heeft `additionalProperties: false` en geen `description`. De eigenschappen nu: `aliases`, `auto-tune-resources`, `command`, `disabled`, `disabled-reason`, `image`, `name`, `path`, `ports`, `resources`, `security`, `services`, `sso-rijk`, `type`, `user-env-vars`, `uses-components`. Schemaversie `2.13` (`x-zad-schema-version`).
- **Het voorbeeld op projectniveau**: `DESCRIPTION_EDITABLE` (`opi/forms/editables/fields/identity.py:18`, `yaml_path="description"`) met de visualizer `DESCRIPTION` (`opi/forms/visualizers/fields/identity.py:20`, `WidgetType.TEXTAREA`, label "Projectomschrijving").
- **Componentvelden**: editables in `opi/forms/editables/fields/components.py` (`COMPONENT_NAME_EDITABLE` op regel 33 en verder), visualizers in `opi/forms/visualizers/fields/components.py` (`COMPONENT_NAME` op regel 33), en de componentsectie in `opi/forms/visualizers/wizard_sections.py` (de lijst rond regel 175 waarin `COMPONENT_NAME` staat).
- **Tonen**: de componentkaarten op de projectpagina, `opi/templates_lotc/bg/project-details.html.j2:39-44` (titel = naam, count = type, sub = poorten).
- **API**: `ComponentDetail` (`opi/api/v2/project_read.py:160`) is wat de v2-API over een component teruggeeft. Schrijven gaat via `AddComponentRequest` (`opi/api/router.py:678`) en `UpdateComponentRequest` (`opi/api/router.py:1084`).

## Wat er moet komen

### 1. Schema, met een schemastap

- `description` op een component in `project_v2.json`: `{"type": "string", "maxLength": 500}`. Optioneel. 500 is een voorstel: genoeg voor een paar zinnen, te kort voor een handleiding.
- Een nieuwe schemaversie volgens `features/project-schema-versions.md` ("Een migratie toevoegen"):
  - een migratiestap in `MIGRATION_STEPS` (`opi/services/schema_migration.py`) die alleen de versie ophoogt;
  - `x-zad-schema-version` naar de nieuwe versie, als string;
  - `opi/schemas/project_legacy/v2.13.json` met een merge patch die `description` van het component weer weghaalt, zodat een 2.13-bestand met dat veld geweigerd blijft.
- Welk nummer: het eerstvolgende vrije. Het mailplan (`plans/een-mailaccount-per-deployment.md`) claimt `2.14`; wie als tweede merget neemt het volgende, zoals de doc zegt. Kijk bij de start op forgejo/main welk nummer vrij is.

### 2. Formulier

- `COMPONENT_DESCRIPTION_EDITABLE` in `opi/forms/editables/fields/components.py`: `yaml_path="components[*]/description"`, niet verplicht, `MinMaxLengthValidator(0, 500)` of wat daar het bestaande equivalent voor een maximum is. Leeg betekent: de sleutel gaat uit het bestand (`remove_when_none=True`, zoals `DEPLOYMENT_ENVIRONMENT_EDITABLE` in `opi/forms/editables/fields/deployments.py`), zodat er geen `description: ''` achterblijft.
- `COMPONENT_DESCRIPTION` in `opi/forms/visualizers/fields/components.py`: `WidgetType.TEXTAREA`, label "Omschrijving", hulptekst "Wat dit component doet, in een of twee zinnen. Optioneel."
- In de componentsectie direct onder de naam, zowel bij toevoegen als bij bewerken. Controleer dat de modal-edit van een component het veld ook rendert en opslaat; `tests/forms/test_modal_noop_roundtrip.py` is de detector voor stille drops in modals en moet groen blijven.

### 3. Tonen

- Op de componentkaart in `project-details.html.j2`: de omschrijving als extra regel onder de poorten, alleen als hij er is. Lang? Dan afkappen met de volle tekst in een `title`, of de opmaak die LOTC daarvoor heeft (`features/lotc-bouwlijn.md`).
- Overal waar een component verder met naam wordt getoond (het componentpaneel, de deploymentweergave): zoek ze op en toon de omschrijving waar er ruimte voor is. Noem in de PR welke plekken je wel en niet hebt meegenomen.

### 4. API

- `ComponentDetail` krijgt `description: str | None`.
- `AddComponentRequest` en `UpdateComponentRequest` krijgen een optioneel `description` met `max_length=500`. Bij update: weglaten laat de bestaande staan, een lege string haalt hem weg.
- Rechten: dit valt onder de bestaande handelingen `component.add` en `component.update` (`opi/core/authz/rules.py`). Er komt geen nieuwe handeling bij.

### 5. Documentatie

- Voeg het veld toe aan `features/components-services-deployments.md` met een voorbeeld. Past het daar niet, maak dan `features/component-omschrijving.md` (wat, hoe, voorbeeld).
- De voorbeeldprojecten in `projects/` hoeven niet mee.

## Tests

- Schema: een component met `description` valideert op de nieuwe versie; een bestand dat `2.13` declareert met `description` op een component wordt geweigerd; langer dan 500 wordt geweigerd.
- Migratie: een 2.13-bestand komt op de nieuwe versie uit zonder inhoudelijke wijziging.
- Formulier: toevoegen met omschrijving schrijft het veld; leegmaken haalt de sleutel weg; de modal-roundtrip laat hem staan.
- API: `ComponentDetail` geeft hem terug; add en update schrijven hem; update zonder het veld laat hem staan.
- Weergave: de kaart toont de omschrijving als hij er is en niets extra's als hij er niet is.

## Afspraken

- Nieuwe code (identifiers, testnamen) in het Engels; commentaar, docstrings en docs in het Nederlands.
- Geen em dashes.

## Buiten scope

- Een omschrijving op een deployment. Kan later langs dezelfde weg, maar is niet gevraagd.
- Markdown of opmaak in de omschrijving: het is platte tekst.
