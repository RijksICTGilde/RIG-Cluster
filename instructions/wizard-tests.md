# Testen van een wizard of een dienstconfig: de vijf niveaus

Er is één indeling voor het testen van alles wat door een formulier het projectbestand in
gaat. Ze stond in een docstring van één testbestand
(`tests/test_service_health_check.py`) en was daarmee onvindbaar; hier staat ze, en hier
hoort ze onderhouden te worden.

Gebruik ze voor twee soorten werk:

- **een dienst**: een nieuwe of gewijzigde dienst met config in de wizard;
- **een wizardflow**: gedrag van de wizard zelf — welke stap wat schrijft, wat er gebeurt
  als een veld leeg blijft, wat "ontbreekt" betekent.

## De vijf niveaus

| Niveau | Wat het bewijst | Waar het staat | Kost |
|---|---|---|---|
| 1 | **Configvalidatie**: een goede config wordt geaccepteerd, een foute AFGEWEZEN met een melding die de dienst noemt | `tests/test_<onderwerp>.py` | ms |
| 2 | **Opslag/rondgang**: een POST gaat door `EditableFormProcessor` en landt op het JUISTE yaml-pad in de JUISTE vorm | `tests/` of `tests/forms/` | ms |
| 3 | **Manifestbijdrage**: de dienst leest de config en levert de templatevariabelen op | `tests/test_<onderwerp>.py` | ms |
| 4 | **Gerenderd manifest**: die variabelen komen echt in de YAML terecht | `tests/test_<onderwerp>.py` | ms |
| 5 | **UI (Playwright)**: de echte wizard rendert de velden en bedraadt ze naar de verwachte payload | `tests/e2e/test_<onderwerp>_wizard.py` | seconden |

**Niveau 2 en niveau 5 vullen elkaar aan; geen van beide vervangt de ander.** Niveau 2 is
snel genoeg om bij elke wijziging te draaien en pint de vorm van de data vast. Niveau 5 is
het enige niveau dat ziet wat de browser werkelijk verstuurt — en dat is precies waar de
bugs van 6 augustus 2026 zaten: een `disabled` checkbox die niets verstuurt, een stap die
alleen in de bewerk-flow misging. Een cluster is voor géén van de vijf nodig.

## De sjablonen

| Wat je bouwt | Kopieer |
|---|---|
| een dienst, niveau 1–4 | `tests/test_service_health_check.py` |
| een dienst, niveau 5 | `tests/e2e/test_service_health_check_wizard.py` |
| een wizardflow, niveau 2 | `tests/forms/test_wizard_base_and_mutations.py` |
| een wizardflow, niveau 5 | `tests/e2e/test_wizard_locked_service.py` |

Kopiëren, hernoemen, en de constanten bovenin op je eigen onderwerp richten. Niet zelf een
nieuwe opzet verzinnen: de waarde zit erin dat elke test op dezelfde plek hetzelfde doet.

## Niveau 2 voor een FLOW, niet alleen voor een dienstconfig

Een dienstconfig-test op niveau 2 voert één POST aan de processor. Een flow-test doet er
één stap meer bij: de verzoening die de router uitvoert. Dat is waar een wizard een basis
en een mutatie combineert, en waar het misgaat.

```python
async def _submit_services(section, base, post):
    """Eén stap-inzending, precies zoals de routers hem draaien."""
    processor = EditableFormProcessor()
    result, errors = await processor.process_json_submission(
        copy.deepcopy(post), section.editables, copy.deepcopy(base)
    )
    assert not errors, f"submit reported errors: {errors}"
    apply_services_mutation(section.editables, base, result)   # wat de router doet
    return result["services"]
```

Drie dingen maken dit een flow-test:

1. **`base` is echt de basis** — het projectbestand zoals het was, niet de POST nog eens.
   Een test die `base` gelijkstelt aan `post` bewijst niets over "ontbreekt".
2. **De POST bevat alleen wat het formulier verstuurt.** Laat weg wat de browser niet
   stuurt (een vergrendeld veld, een dichtgeklapte sectie). Dat weglaten ís de test.
3. **Draai dezelfde inzending door BEIDE flows** met
   `@pytest.mark.parametrize("section", [SERVICES_SECTION, SERVICES_EDIT_SECTION])`.
   Elke regel die aan een sectienaam hangt valt hier om. Twee bugs op één dag waren dit.

## Regel: mutatietoets je bewaker

Een test die groen is zonder de reparatie bewaakt niets. Zet de fix tijdelijk uit en
controleer dat de test rood wordt; zet hem terug. Doe dit één keer, bij het schrijven.

## Draaien

```bash
cd operations-manager/python
uv run pytest tests/forms/test_wizard_base_and_mutations.py -q          # niveau 2
uv run pytest tests/e2e/ -m "e2e and not sandbox" -q                    # niveau 5, lokaal
```

Niveau 5 vraagt eenmalig `uv run playwright install chromium`. In een container ook
`uv run playwright install-deps chromium`.

Schrijf een niveau-5-test die niet leunt op wat ervoor draaide: `task test-e2e-random`
schudt de bestanden en de tests daarbinnen door elkaar, en CI draait die vorm. Een test die
zijn beginstaat van een vorige test krijgt, valt daar om -- terecht. Zie
`features/e2e-in-ci.md`.

## Dekking vandaag

Gemeten 6 augustus 2026 over de dienstpakketten: 2x niveau 1, 4x niveau 2, 3x niveau 3,
3x niveau 4, 6x niveau 5. De indeling was dus wel bedacht en nauwelijks toegepast. Bij een
nieuwe dienst of een wijziging aan de wizard hoort dat aantal omhoog te gaan, niet het
aantal diensten.
