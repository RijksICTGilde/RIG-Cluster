# Een webadres wijzigen liep vast op een TLS-keuze die je niet kon waarmaken

De opdrachtgever wilde het webadres van deployment `pr-19` in project `toets-hn7` wijzigen naar een eigen domein. Dat lukte niet. Het scherm gaf:

```
Project 'toets-hn7': configuratie van service 'publish-on-web' in component 'frontend' van
deployment 'production' is ongeldig: 1 validation error for PublishOnWebComponentConfig Value
error, tls 'provided' requires an 'attachment' naming the certificate [type=value_error,
input_value={'tls': 'provided'}, input_type=dict] For further information visit
https://errors.pydantic.dev/2.12/v/value_error. Geaccepteerde velden: tls, attachment.
```

Het is uiteindelijk gelukt door in het TLS-veld expliciet "Standaard certificaat" te kiezen. Er zijn hier **vier dingen** mis, en ze versterken elkaar. Repareer ze samen; los repareren van een ervan laat het scherm net zo verwarrend achter.

## Wat er gemeten is

**Het projectbestand bevat nergens `provided`.** In `projects/toets-hn7.yaml` (gecontroleerd in de sandboxrepository en in de warme kopie op productie, allebei gelijk, bestand van 08:51 die ochtend): de root-component `frontend` heeft `publish-on-web` met `{"tls": "standard"}`, deployment `test` heeft een `domain-format`, en verder heeft geen enkele deployment een eigen `publish-on-web`-configuratie op componentniveau. `production/frontend` heeft `services: null`. De waarde uit de melding ontstaat dus in het FORMULIER, niet in het bestand.

**Het project heeft geen bijlagen.** En `provided` eist er een: `PublishOnWebComponentConfig._provided_needs_an_attachment` (`opi/services/catalog/publish_on_web/config_model.py:193`) weigert `tls: provided` zonder `attachment`.

**Toch wordt `provided` gewoon aangeboden.** `PublishTlsModeOptionsProvider` (`opi/forms/visualizers/providers.py`) geeft altijd dezelfde drie modi terug: standard, passthrough, provided. En het bijlageveld dat daarnaast verschijnt (`PUBLISH_ON_WEB_ATTACHMENT_EDITABLE`, `show_when={"value": ["provided"]}`) biedt bij een leeg overzicht alleen `{"value": "", "label": "Geen bijlagen geüpload: upload eerst op de Bijlagen-sectie"}`.

Dat is een doodlopend scherm: je kunt een modus kiezen, en er is vervolgens geen enkele waarde waarmee je aan de eis kunt voldoen.

## De vier dingen die opgelost moeten worden

**1. `provided` mag niet te kiezen zijn zonder bijlage.** De voor de hand liggende reparatie is de optie weglaten zolang de bijlagencatalogus leeg is, in de values provider, zoals de rest van dit soort keuzes al werkt. Overweeg ook de vriendelijker variant: hem tonen maar uitgeschakeld, met als reden "upload eerst een certificaat bij Bijlagen". Kies met een reden; een optie die stil verdwijnt is voor wie hem zoekt ook verwarrend. Dit geldt op ALLE lagen: het component én de per-deployment override (`PublishTlsOverrideOptionsProvider`).

**2. De standaardwaarde is "erven", en dat is niet te zien.** De opdrachtgever moest expliciet "Standaard certificaat" aanvinken om verder te komen. Zoek uit wat er precies gebeurt bij de lege (erf-)waarde in dit scherm, en waarom die hier tot `provided` leidde terwijl de component `standard` zegt. **Dit is het minst begrepen punt en waarschijnlijk de eigenlijke oorzaak**; de andere drie maken het zichtbaar en onherstelbaar. Meet het, gok niet.

**3. De fout gaat over de verkeerde deployment.** Er werd `pr-19` bewerkt; de melding gaat over `production`. Ook als de validatie terecht ingrijpt, hoort een bewerking op één deployment een andere niet te raken. Zoek uit of het formulier werkelijk naar `production` schrijft of dat alleen de melding de verkeerde naam noemt. Dat verschil is groot: het eerste is dataverlies-in-de-dop, het tweede is een verkeerde tekst.

**4. De melding is rauwe pydantic-uitvoer.** `opi/manager/project_validation.py:107` zet `{e}` in de tekst, dus de gebruiker krijgt `[type=value_error, input_value=..., input_type=dict]` en een link naar `errors.pydantic.dev`. **Twee regels verderop staat het al goed**: regel 130 en 309 gebruiken `"; ".join(error["msg"] for error in e.errors())`. Trek die vorm door, en zeg erbij wat de gebruiker moet DOEN ("kies Standaard certificaat, of upload eerst een certificaat bij Bijlagen"), niet alleen wat er mis is.

## Wat er buiten valt

- De validatieregel zelf. `tls: provided` zonder bijlage levert bij het renderen geen certificaat op; die hoort tegengehouden te worden. Het probleem is dat je in die toestand terecht kunt komen en er niet uit komt.
- De TLS-modi zelf, en passthrough.

## Verifieerbaar

- Een project ZONDER bijlagen: het TLS-veld leidt niet meer tot een toestand die je niet kunt opslaan. Toets dat via het formulier, niet alleen via het model.
- Een project MET bijlagen: `provided` is gewoon te kiezen en de bijlage is te selecteren.
- Een test die het gemeten geval nabouwt: het webadres van één deployment wijzigen op een project waarvan een ándere deployment geen publish-on-web-configuratie heeft, en dat dat slaagt.
- De melding bevat geen `type=value_error`, geen `input_value=` en geen pydantic-URL meer, en noemt wat je moet doen.
- `uv run pytest tests/ -q` groen, plus `ruff check`, `ruff format`, `pyright`.
