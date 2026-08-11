# Hoe een aanvinkvakje werkt

Eén verhaal over het aanvinkvakje, met de meting erbij. Er stonden er twee in de
codebase en die spraken elkaar tegen; dit vervangt ze allebei.

Aanleiding: "Toegang beperken" in de keycloak-configstap was niet uit te zetten.
Aanvinken lukte, uitvinken niet - het vakje sprong terug. Dat was de derde keer dat een
aanvinkvakje terugkwam.

## De vorm

**Eén vorm, overal.** Een aanvinkvakje is `<c-checkbox-field>`:

```jinja
{# een enkel vakje - templates_lotc/widgets/checkbox.html.j2 #}
<c-checkbox-field id="{{ pad }}" name="{{ pad }}" label="..." value="true"
                  :checked="field.value" :attrs="dict(field_attrs(field), id=field.path)" />

{# een groep - templates_lotc/widgets/checkbox_group.html.j2 #}
<c-checkbox-field id="{{ pad }}" label="...">
    <c-checkbox name="{{ pad }}[]" value="pvc" label="PVC" :checked="..." />
</c-checkbox-field>
```

Onder NLDD worden die allebei hetzelfde element: `<nldd-checkbox-field>`. Een groep en
een enkel vakje gedragen zich daarom ook hetzelfde. Voor die samenvoeging had de groep
kale `<input>`'s met eigen CSS (`.lotc-checkbox-regel`), en dat is waar de verwarring
vandaan kwam.

**Geen eigen `<input>` ernaast.** De enige uitzondering staat in
`templates_lotc/widgets/service_cards.html.j2` en is daar gemeten: de dienstkaart is ons
eigen onderdeel met eigen CSS, `wizard.js` zoekt zijn besturingselement met
`querySelector('input[type="checkbox"]')`, en een poging het om te zetten liet 7 van de 9
tests vallen op het slot, het terugdraaien en het bewaren van een presetkeuze.

## Wat het element doet (gemeten)

In de browser gemeten op `lord-of-the-components @ 4307413`, NLDD-thema, op de
keycloak-configstap van de wizard:

| vraag | antwoord |
|---|---|
| `<input>` in de lichte boom? | nee |
| `<input>` in de schaduwboom? | ja, twee diep: `nldd-checkbox-field` -> `nldd-checkbox` -> `input` |
| staat het in `form.elements`? | ja - het is form-associated (ElementInternals) |
| `element.checked` | volgt de klik |
| `new FormData(form)`, vakje AAN | de sleutel staat erin, met `value="true"` |
| `new FormData(form)`, vakje UIT | de sleutel staat er NIET in |

Het component doet het dus goed: `formValue()` geeft `this.checked ? this.value : null`, en
`FormData` neemt dat over.

Let op de tweede rij: `el.shadowRoot.querySelector('input')` geeft `null` en dat leest als
"er is geen invoerveld". Er is er wel een, één schaduwboom dieper.

## Waar het misging: htmx

htmx (1.x) verzamelt zijn parameters **niet** met `FormData`. Het loopt `form.elements` af
en leest per element `.name` en `.value`, met één uitzondering:

```js
if (e.type === "checkbox" || e.type === "radio") { return e.checked }
```

Een form-associated custom element heeft geen `.type`, dus die uitzondering slaat niet
aan. htmx neemt het altijd mee en leest zijn `.value` - en die is `"true"`, aangevinkt of
niet. Met het vakje UIT gemeten:

```
new FormData(form)  ->  geen sleutel
htmx.values(form)   ->  {"_services-config/keycloak/config/restrict-access/enabled": "true"}
over de lijn        ->  {"restrict-access": {"enabled": "true"}}
```

Daar komt "aanvinken lukt, uitvinken niet" vandaan: uitvinken stuurde nog steeds `true`,
de server sloeg `true` op, en het hertekende formulier zette het vakje netjes weer aan.

### De oplossing

`static/js/form-associated.js` haakt in op `htmx:configRequest` en neemt voor elke naam
die door een form-associated custom element wordt gedragen het oordeel van `FormData`
over. Dat is de standaard, het vraagt het element zelf om zijn formulierwaarde, en het
geldt voor elk zo'n veld - niet alleen voor aanvinkvakjes en niet alleen voor het
component van vandaag.

Twee regels die er niet uit mogen:

- **alleen corrigeren, nooit toevoegen.** Een naam die htmx zelf niet meestuurde blijft
  weg. htmx laat de waarden van het omliggende formulier bij een GET bewust liggen, en
  een veld daar alsnog inhangen maakt van de knop "Vorige" (een `hx-get` binnen het
  formulier) een verzoek met formuliervelden in de URL. De wizard weigert dat met een 400
  - zo ziet een formulier eruit dat naar een GET is teruggevallen.
- **geen waarde betekent: sleutel weg.** Precies wat een gewoon `<input type="checkbox">`
  doet, en wat de server als "uit" leest.

Het script staat naast htmx in `base_lotc.html.j2`, niet naast `json-enc.js`: het geldt
voor elk htmx-verzoek uit een formulier, ongeacht de codering. (`json-enc.js` verzamelt
zelf niets; het krijgt de parameters van htmx en zet ze alleen om naar geneste JSON.)

## Waar het daarna nóg misging: de server

Met de browser hersteld kwam de tweede helft bloot te liggen. Een stapfragment wordt
ADDITIEF over de basis gemerget (`get_merged_data`, en voor diensten met
`merge_service_lists`, die de config deep-merget). Een sleutel die er simpelweg niet is
kan de oude waarde dus niet verwijderen: die komt gewoon terug.

De wizard heeft daar een grafsteen voor (`CLEARED_FIELD`), maar die werd alleen gezet voor
velden in een indexlijst (`deployments[0]/...`), niet voor dienstconfig. Sinds RC-71 doet
`_tombstone_service_config` in `opi/web/router_wizard.py` dat ook voor dienstconfig:

- alleen voor velden die de sectie zelf schrijft, en die de verwerker echt heeft
  leeggemaakt - wat hij oversloeg (readonly, of verborgen door `show_when`) staat nog in
  zijn resultaat en blijft dus met rust;
- niet voor een SEQUENCE: die draagt zijn eigen items en beslist zelf wat leeg betekent;
- op het diepste niveau dat er nog is. Een veldpad dat een tussenlaag mist zou die anders
  aanmaken, en dan levert opslaan een leeg `restrict-access: {}` op - een wijziging die de
  gebruiker niet maakte.

`get_merged_data` haalt de gemarkeerde sleutel na het mergen weg; bij het opslaan
verwijdert `apply_write_paths` hem uit het projectbestand.

## Het vakje terugvinden

`[id='<pad>']` is de enige selector die precies het vakje oplevert.

Op de NAAM zoeken kan niet. Playwright kijkt door schaduwbomen heen en het element geeft
zijn name aan zijn schaduwelementen door, dus `[name='<pad>']` levert er drie op:

```
NLDD-CHECKBOX-FIELD   (licht)
NLDD-CHECKBOX         (schaduw van NLDD-CHECKBOX-FIELD)
INPUT                 (schaduw van NLDD-CHECKBOX)
```

Op `input[id=...]` zoeken - wat `veldbesturing()` voor tekstvelden doet - kan ook niet: de
`<input>` zit in de schaduwboom en draagt geen id. Daarom zetten de widgets de id via de
attribuutbundel op het besturingselement zelf; de component gebruikt hem verder alleen om
zijn hulptekst en foutmelding aan te knopen.

Voor tests staan er twee helpers in `tests/e2e/helpers/wizard.py`:

```python
aanvinkvakje(page, "_services-config/keycloak/config/restrict-access/enabled")  # één vakje
aanvinkvakjes(page, "resource_types")                                          # de vakjes van een groep
```

`aanvinkvakjes` matcht op naam EN id: een prefixselector op `<pad>-` pikt ook de hulptekst
(`<pad>-help`) mee en telt dan een vakje te veel.

De stand lees je van het element: `el.checked`. Niet `is_checked()`, want dat wil een
`<input>` zien.

## De toets

`tests/e2e/test_aanvinkvakje.py` doet het hele rondje - aanvinken, opslaan, heropenen,
uitvinken, opslaan, heropenen - en toetst daarna het projectbestand zelf. Plus twee tests
op wat er over de lijn gaat, op beide vormen, want alleen daar is te zien dat het aan de
verzendkant zat.

Alles toetste tot nu toe AANzetten. Wat er misging was UITzetten.

```bash
uv run pytest tests/e2e/test_aanvinkvakje.py -m "e2e and not sandbox" -q
```
