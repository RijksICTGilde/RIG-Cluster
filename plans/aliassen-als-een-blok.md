# Aliassen worden één blok

Aliassen worden nu **per sleutel** versleuteld opgeslagen: `aliases` is een mapping waarvan elke waarde een eigen AGE-blok is. Dat gaat weg. Vanaf deze wijziging is `aliases` net als `user-env-vars` **één geheel**: één AGE-blok waarvan de platte tekst `KEY=value`-regels zijn.

Lezen moet twee vormen aankunnen: **onversleuteld** (zoals het altijd al mocht, en nog steeds mag) en **één AGE-blok**. De vorm per sleutel wordt niet ondersteund en er wordt geen migratie voor bedacht.

## Waarom

Het onderscheid tussen de twee opslagvormen was nooit een keuze over aliassen zelf, en het lekte overal doorheen:

- Op de componentkaart stond een AGE-blok als waarde van `POSTGRES_HOST`, omdat de pagina niet ontsleutelde.
- In het bewerkveld stond letterlijk `__opi-redacted-secret__`, omdat de sessieredactie afdaalde in de mapping en per aliasnaam oordeelde. Een naam als `POSTGRES_HOST` kent geen editable, dus alles eronder werd weggestreept. `user-env-vars` ontsprong die dans alleen omdat dat één blok is en dus nooit een afdaling werd.

Beide zijn inmiddels los gerepareerd (commit `a817deda`), maar de reparaties bestaan alleen omdat de opslagvorm afwijkt. Eén vorm voor beide eigenschappen laat ze allebei vervallen.

Er is nog een reden. De dienst zegt zelf dat een alias die naar platformvariabelen verwijst géén geheim is (`AliasesService.owned_value_is_secret`: "a reference is the coupling itself, not a secret"), en de validator wéigert een alias zonder verwijzing. Elke geldige alias is dus een verwijzing, en de versleuteling per waarde beschermde iets wat per definitie niet geheim is; ze maakte alleen elke lezer afhankelijk van een ontsleutelstap. Als één blok blijft dat gedrag hetzelfde als bij `user-env-vars`, wat de gebruiker verwacht, en het is niet meer per lezer te vergeten.

## Wat er nu is

`ValueStorage` (`opi/services/catalog/base.py:65`) kent twee vormen en `opi/services/component_values.py` is de enige implementatie van allebei:

- `BLOCK` — de hele set is één AGE-blok met `KEY=value`-regels (`user-env-vars`).
- `PER_VALUE` — een mapping met leesbare namen en elke waarde apart versleuteld (`aliases`).

`AliasesService.owned_values_storage = ValueStorage.PER_VALUE` staat op `opi/services/catalog/aliases/__init__.py:55`.

## Taken

### 1. De opslagvorm omzetten

- `opi/services/catalog/aliases/__init__.py:55` → `owned_values_storage = ValueStorage.BLOCK`.
- `ValueStorage.PER_VALUE` en `_decode_per_value` vervallen zodra niemand ze meer noemt. **Verwijder ze pas als taak 2 tot en met 5 klaar zijn**, dan wijst de typecontrole elke overgebleven aanroep aan.

Verifieer: `rg "PER_VALUE" opi/` levert niets meer buiten de eventuele leesvariant uit taak 3.

### 2. Het schema

`opi/schemas/project_v2.json`, `$defs/component/properties/aliases`, is nu:

```json
{"type": "object", "additionalProperties": {"type": "string"}}
```

Dat moet er een string naast krijgen, want één AGE-blok is een string:

```json
{"oneOf": [
  {"type": "object", "additionalProperties": {"type": "string"}},
  {"type": "string"}
]}
```

De mapping blijft geldig: dat is de onversleutelde vorm, en die mag blijven bestaan.

Verifieer: een project met een aliasblok als string komt door `validate_project_schema`, en een project met een onversleutelde mapping ook. Let op de les uit dp-bn7: valideer op de **gemigreerde** data (`migrate_to_latest()` en dán valideren), anders mis je het gat.

### 3. Lezen

`decode()` in `opi/services/component_values.py` stuurt op de opgegeven `storage`. Met `BLOCK` komt het uit bij `_decode_block`, en die kan al twee van de drie vormen aan: een AGE-blok (ontsleutelen en als `KEY=value` parsen) en platte tekst. De derde tak is de mapping:

```python
if isinstance(raw, dict):
    # Legacy mapping shape, from before the value became a single string.
    return {str(key): str(value) for key, value in raw.items()}
```

Die geeft de waarde terug zoals hij staat. Voor een **onversleutelde** mapping is dat goed en dat moet zo blijven.

**Openstaande beslissing.** Voor een mapping waarvan de waarden AGE-blokken zijn (wat er vandaag in de sandbox staat, bijvoorbeeld `sd-ugy`) geeft die tak de ciphertext terug, en dan staat er weer een AGE-blok op het scherm. Twee wegen:

- **(a) Laat staan.** De vorm per sleutel wordt niet ondersteund; wie zo'n project heeft, ziet ciphertext tot het component één keer wordt opgeslagen. Precies wat er is afgesproken: geen oplossing verzinnen voor de halve vorm.
- **(b) Drie regels in die tak** die een waarde ontsleutelen als `is_age_encrypted(value)`. Dat voegt geen ondersteunde schrijfvorm toe (schrijven doet alleen nog blokken) en houdt bestaande sandboxprojecten leesbaar zonder ze aan te raken.

Voorstel: **(b)**, met een commentaar dat het lezen is en geen vorm die nog geschreven wordt. Het kost drie regels en het voorkomt dat precies de melding terugkomt die deze wijziging veroorzaakte. Wie (a) wil, moet accepteren dat `sd-ugy` er kapot uitziet tot iemand het component opslaat.

### 4. De schrijvers en de lezers langs

`aliases` is op tien plekken in de code een mapping waar `.items()` overheen loopt. Met een AGE-blok is het een **string**, en dan lopen die stil leeg of vallen ze om. Elke plek moet door `decode()`:

| Plek | Wat er staat |
|---|---|
| `opi/manager/project_manager.py:1161` | `component_definition.get("aliases", {})` en daarna `.items()` — **de belangrijkste**: dit is het deploypad, hier worden de aliassen echt gebruikt |
| `opi/manager/project_manager.py:1258` | `component.get("aliases", {})` bij het afbakenen per component |
| `opi/api/v2/project_read.py:262` | `_component_aliases`, ontsleutelt nu expliciet met `ValueStorage.PER_VALUE` |
| `opi/web/router.py:1460` | de componentkaart, sinds `a817deda`, ook `PER_VALUE` |
| `opi/web/router_wizard.py:2601` | `_literalize` per aliasnaam; met een blok is het één `_literalize(comp, "aliases")`, net als de regel erboven voor `user-env-vars` |
| `opi/forms/editables/generators.py:259` | `component.get("aliases")` |
| `opi/utils/project_utils.py:303` | schrijft `component_config["aliases"] = aliases_dict` |
| `opi/core/task_handlers_components.py:81` | `payload.get("aliases")` |
| `opi/api/v2/router.py:3426` e.v. | stuurt al op `storage`, hoeft niets |
| `opi/forms/wizard/secrets.py` | de redactie klopt nu al; met een blok wordt het weer de eenvoudige tak |

Verifieer per plek: geen `.items()` meer op iets wat een string kan zijn. Het deploypad is de zwaarste: een alias die stil wegvalt levert een draaiende pod met een ontbrekende variabele, en dat zie je pas als de applicatie omvalt.

### 5. Schrijven

`encode()` doet met `BLOCK` al het juiste (`"\n".join(f"{key}={value}")` en dan één `encrypt_age_content_sync`). Let op `validate_value_for_storage`: de `BLOCK`-tak doet een `_block_roundtrip`, dus een waarde met een `=` of een regeleinde wordt geweigerd. Voor verwijzingen als `$DATABASE_SERVER_HOST` is dat geen bezwaar, maar de foutmelding moet uitleggen wat er mis is en niet alleen dát er iets mis is.

### 6. Testen

Bestaand, moet meebewegen: `tests/test_collect_deployment_aliases.py`, `tests/test_component_values.py`, `tests/test_component_values_api.py`, `tests/test_component_values_read_api.py`, `tests/test_component_values_manager.py`, `tests/test_alias_reference_validation.py`.

Nieuw, en dit is de poort die ontbrak:

1. **Een rondje door de opslag.** Schrijf aliassen, lees ze terug, en krijg dezelfde mapping. Op de opgeslagen vorm assertie doen: het is één string en geen mapping.
2. **Beide leesvormen.** Een onversleutelde mapping en een AGE-blok leveren allebei dezelfde waarden op.
3. **Het deploypad.** Een component met aliassen als blok levert bij het genereren dezelfde variabelen als datzelfde component met een onversleutelde mapping. Dit is de test die het stille wegvallen vangt, en de reden dat taak 4 het zwaarste punt is.
4. **De componentkaart.** `tests/test_lotc_componentkaart_waarden.py` bestaat al en leest de fixture `opi/web/lotc_fixtures/voorbeeld-volledig.yaml`, waar de aliassen een onversleutelde mapping zijn. Die moet groen blijven: dat is meteen de dekking voor de onversleutelde vorm op het scherm.

### 7. Documentatie

`instructions/services.md` beschrijft de dienstenhaken; `owned_values_storage` hoort daar met de nieuwe stand in. `ValueStorage` in `opi/services/catalog/base.py` beschrijft in zijn docstring nog twee vormen ("Both properties that exist today ... are encrypted differently") — dat klopt na deze wijziging niet meer en moet de reden vertellen waarom de enum nog bestaat, of samen met `PER_VALUE` verdwijnen.

## Wat er buiten valt

- Geen migratie van bestaande projecten met de vorm per sleutel. Wie zo'n project heeft, schrijft het bij de eerste bewerking als blok weg.
- Geen wijziging aan `user-env-vars`: dat is al een blok en blijft dat.
- Geen wijziging aan de vraag óf een alias versleuteld hoort te worden. Dat mag apart, met het argument uit "Waarom": de validator dwingt een verwijzing af en een verwijzing is per onze eigen regel geen geheim. Nu niet meenemen, want het verandert de zichtbaarheid in git en dat is een eigen afweging.

## Volgorde

1. Schema (taak 2) → verifieer: beide vormen valideren.
2. Lezen (taak 3) → verifieer: alle drie de vormen leveren dezelfde mapping.
3. De opslagvorm om (taak 1) → verifieer: de typecontrole wijst de overgebleven aanroepen aan.
4. Alle plekken langs (taak 4 en 5) → verifieer: geen `.items()` op iets wat een string kan zijn; deploytest groen.
5. Testen en documentatie (taak 6 en 7).
