# Eén weg waarlangs een wizard met velden omgaat

## Wat het is

Een flow mag precies die paden in het projectbestand schrijven die zijn eigen
editables noemen. Alles daarbuiten wordt niet aangeraakt - niet overschreven, niet
hersteld, niet "behouden": het komt gewoon niet langs. Dat heet hier de
**schrijfverzameling** van een flow, en hij wordt afgeleid, niet onderhouden.

Twee dingen maken dat mogelijk:

1. **De flow verklaart waar hij schrijft.** `FormFlow.target` zegt welk lijstitem
   een flow bewerkt (`FlowTarget("components", 3)`), in plaats van dat de index uit
   de tekst van de `flow_id` wordt teruggerekend.
2. **De editables verklaren welke velden dat zijn.** Elke `Editable` draagt al een
   `yaml_path`. De verzameling paden van de actieve secties ís daarmee de
   schrijfverzameling.

## Waarom

De opslag keek niet naar de editables. Hij nam alles mee wat in de wizard-sessie
zat en zocht achteraf uit wat er weer af moest. Daar zijn vier lekken door
ontstaan, elk apart gevonden en apart gedicht: een leeggemaakt veld dat terugkwam,
elke serviceconfig-wijziging op projectniveau die verdween, een tweede
serviceconfig-wijziging die verdween, en versleutelde waarden die hun blokopmaak
verloren. De velden een voor een benoemen houdt geen stand; default-deny vanuit de
flow zelf wel.

## Hoe het werkt

`opi/forms/wizard/save.py` is de hele weg van "gebruiker drukt op opslaan" naar
"dit gaat naar schijf". Geen I/O: de router leest uit git en schrijft terug, de
samenvoeging zelf is op gewone dicts te testen.

```
apply_modal_edit(existing_data, merged_data, flow=..., active_sections=...)
  |
  +-- nieuw lijstitem?  -> in zijn geheel toevoegen (er was nog niets te beschermen)
  +-- schrijfverzameling ophalen: flow_write_paths(active_sections)
  +-- alleen die paden schrijven: apply_write_paths(...)
  +-- grafstenen weg, post_merge-haken, afgeleide waarden, PRE_SAVE-haken
  +-- transients strippen, AGE-waarden als blok, resultaat terug
```

### De schrijfverzameling (`opi/forms/wizard/write_set.py`)

| Regel | Betekenis |
|---|---|
| Pad per editable | `yaml_path`, inclusief kinderen van groepen en sequences |
| Afgekapt bij de eerste `[*]` | `components[*]/name` schrijft de hele lijst `components` |
| Genest pad valt weg | Staat een pad al binnen een ander pad, dan schrijft de buitenste het al |
| Ondiep eerst | De lijst wordt geschreven voordat een veld erbinnen wordt gezet |

Bij het schrijven zijn er drie gevallen per pad:

- **Waarde aanwezig** in de ingestuurde data: schrijven.
- **Grafsteen** (`CLEARED_FIELD`): de gebruiker heeft het veld leeggemaakt, dus
  wissen. Een grafsteen blijft nodig - een ontbrekende sleutel kan een verwijdering
  niet uitdrukken.
- **Afwezig terwijl een container erboven wél is ingestuurd**: ook leeggemaakt, dus
  wissen. Zonder dat onderscheid komt een gewiste waarde bij de volgende opslag
  terug.

Wat níét wordt gewist: een top-level sleutel die de flow deze keer niet draagt (een
flow die geen `services` meebrengt mag de services van het project niet laten
vallen), en een service-pad zonder subpad (dat zou de hele dienst deselecteren, een
andere beslissing dan het legen van één configveld).

### Het doelwit van een flow (`opi/forms/visualizers/flows.py`)

```python
FormFlow(flow_id="modal-edit-component-3", ..., target=FlowTarget("components", 3))
```

`INDEXED_FLOWS` beschrijft per flow-familie één keer wat de rest nodig heeft: het
prefix, de lijst waarin hij schrijft, hoe je hem bouwt voor een index, of hij een
nieuw item toevoegt, en wat de bouwer uit de wizard-sessie nodig heeft
(`component_count`, `is_new`). `get_flow`, het bijvullen van dunne formulierdata,
het aanmaken van een lege plek en het bepalen van de doel-deployment lezen die
verklaring in plaats van de tekst van de `flow_id`.

### De API valideert uit dezelfde editables

`opi/api/validation.py` wijst per veld naar de gedeelde editable, niet naar een
eigen kopie. Alleen "verplicht" is per endpoint (`_required` / `_optional`): of een
veld weggelaten mag worden is de vraag van het endpoint, hoe de waarde eruit moet
zien die van het veld.

## Een nieuwe flow toevoegen

1. Bewerkt hij één lijstitem? Zet dan `target=FlowTarget(<lijst>, <index>)` op de
   `FormFlow`, en zet de familie in `INDEXED_FLOWS`.
2. Verder niets. De schrijfverzameling volgt uit de secties die je meegeeft.
3. Moet een veld geschreven worden dat je niet toont? Dan hoort daar een editable
   bij (eventueel `readonly`), of een hook die na de samenvoeging schrijft. Een
   veld dat nergens verklaard is, wordt niet geschreven - dat is de bedoeling.

## Waar het door bewaakt wordt

`tests/forms/test_flow_write_isolation.py` doet per flow twee dingen op een
realistisch projectbestand:

- **Eén veld wijzigen**: de gedumpte YAML moet gelijk zijn aan hetzelfde bestand
  waarin alleen dat veld met de hand is aangepast. Byte voor byte, dus ook de
  sleutelvolgorde.
- **Niets wijzigen**: door de flow lopen en opslaan moet het bestand exact laten
  zoals het was. Dat is de terugreis van elk veld dat wél getoond maar niet
  getypt mag worden.

`tests/test_modal_edit_nondestructive.py` draait daarnaast een
backup-schema- en domeinbewerking over alle echte projectbestanden in de repo.

## Belangrijke bestanden

| Bestand | Rol |
|---|---|
| `opi/forms/wizard/save.py` | `apply_modal_edit` - de hele opslagweg, zonder I/O |
| `opi/forms/wizard/write_set.py` | `flow_write_paths` / `apply_write_paths` |
| `opi/forms/visualizers/flows.py` | `FlowTarget`, `INDEXED_FLOWS`, `get_flow` |
| `opi/forms/editables/merge.py` | de enige diepe samenvoeging van het formulierpad |
| `opi/api/validation.py` | API-profielen, afgeleid van dezelfde editables |
| `opi/web/router_detail_edit.py` | routes; leest en schrijft git rond `apply_modal_edit` |

Zie ook [edit-modal-config-chaining.md](edit-modal-config-chaining.md) voor de
stapnavigatie van de modal en [editables-framework.md](editables-framework.md) voor
de editables zelf.
