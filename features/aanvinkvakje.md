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
                  :checked="field.value" :attrs="field_attrs(field)" />

{# een groep - templates_lotc/widgets/checkbox_group.html.j2 #}
<c-checkbox-field id="{{ pad }}" label="..." :attrs="dict(field_attrs(field), id=field.path)">
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

## Waar het daarna nog eens misging: de tekenkant

Twee keer htmx en de server, en toch stond er in de schemastap van de wizard een vakje
AAN ("Markeer voor verwijdering") bij een schema dat `marked-for-deletion: false` heeft.
Deze keer zat het aan de andere kant: niet in wat er verstuurd wordt, maar in wat er
getekend wordt.

`editable_to_form_field` (`opi/forms/visualizers/bridge.py`) koos per widget tussen twee
omzettingen van de opgeslagen waarde:

| omzetting | waarvoor | wat een BooleanConverter oplevert |
|---|---|---|
| `read()` | select, text, textarea, radio | `"true"` / `"false"` |
| `view()` | al het andere | `"Ja"` / `"Nee"` |

Een aanvinkvakje stond in geen van beide lijstjes en viel dus in `view()`. Het sjabloon
toetst `:checked="field.value"`, en `"Nee"` is een niet-lege tekst. Elk aanvinkvakje met
een converter stond aan, ongeacht de waarde - ook "Versiebeheer op de bucket" bij minio.

De oplossing staat op dezelfde plek: een aanvinkvakje krijgt een ECHTE boolean, uit
dezelfde reeks waarden die `BooleanConverter.write()` gebruikt, zodat tonen en opslaan het
over hetzelfde eens zijn.

```python
if widget == "checkbox":
    display_value = raw_value in (True, "true", "on", "yes", "1")
```

Gemeten in de browser op de schemastap: vóór de reparatie stond het vakje van een verse
rij aan, erna uit. De toets staat in `tests/forms/test_aanvinkvakje_stand.py`.

## Twee elementen met dezelfde id

Hier hoorde nog een tweede fout bij, die de toetsenbordtest hierboven al liet vallen:
`[id='<pad>']` leverde er TWEE op. Het `id`-attribuut van `<c-checkbox-field>` landt
namelijk op de omhulling, en de attribuutbundel (`:attrs`) op het besturingselement - en
in beide stond het veldpad.

Dat is bij de BRON opgelost en niet met een tweede id aan onze kant: sinds LOTC `762e570`
zet het component de id zelf op het besturingselement (`<nldd-checkbox-field>`) in plaats
van op de omhulling, en wij geven hem alleen nog als prop mee - niet ook in `:attrs`. Het
vakje draagt dus `<pad>` en niets anders draagt dat pad; de hulptekst en de foutmelding
heten `<pad>-help` en `<pad>-error`, want die worden uit dezelfde prop samengesteld.

**Voor een GROEP geldt het omgekeerde**, en dat is met opzet. Die 762e570 raakt alleen het
enkele vakje; een groep rendert een andere componentvorm (`<nldd-form-field>` met de vakjes
als kinderen) en die zet de id-prop nergens als attribuut neer. Zonder de attribuutbundel
draagt dus GEEN enkel element het veldpad meer en is de groep niet meer met `[id='<pad>']`
te vinden - vier tests in `tests/e2e/test_gedragsoppervlak.py` vielen daarop om. In
`checkbox_group.html.j2` staat de id daarom wél in `:attrs`; hij landt op de omhullende div
en er is niets dubbels, want de prop landt nergens. De vakjes zelf houden hun eigen
`<pad>-<waarde>`.

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
`<input>` zit in de schaduwboom en draagt geen id.

De id landt daarom op het besturingselement, en op niets anders. Dat was een tijd lang
niet zo: het component zette hem op de omhullende `div.lotc-checkbox-field` en wij zetten
hem daarnaast via de attribuutbundel op `<nldd-checkbox-field>`, dus ELK enkel vakje leverde
twee elementen met dezelfde id op (gemeten in de generale repetitie,
`docs/generale-repetitie-2026-08-12.md`, bevinding 2). Dat is ongeldige HTML en het breekt
`label for=` en `aria-describedby`. Sinds LOTC `762e570` zet het component de id zelf op
`<nldd-checkbox-field>`; wij geven hem alleen nog als prop mee. De hulptekst en de
foutmelding houden hun `<pad>-help` / `<pad>-error`, want die worden uit de prop
samengesteld. De vangrail staat in `tests/test_lotc_aanvinkvakje_id.py`.

Voor tests staan er twee helpers in `tests/e2e/helpers/wizard.py`:

```python
aanvinkvakje(page, "_services-config/keycloak/config/restrict-access/enabled")  # één vakje
aanvinkvakjes(page, "resource_types")                                          # de vakjes van een groep
```

`aanvinkvakjes` matcht op naam EN id: een prefixselector op `<pad>-` pikt ook de hulptekst
(`<pad>-help`) mee en telt dan een vakje te veel.

De stand lees je van het element: `el.checked`. Niet `is_checked()`, want dat wil een
`<input>` zien.

## Het vakje vanuit eigen JavaScript aansturen

Dit is het lastigste deel, en het komt terug bij elk web-component dat een eigen
besturingselement in zijn schaduwboom tekent. De dienstenkiezer in de wizard doet het:
hij vinkt afhankelijkheden automatisch aan en hij WEIGERT het uitvinken van een dienst die
een andere dienst nodig heeft.

### Drie standen die het eens moeten zijn

Bij `<nldd-checkbox>` bestaat "aangevinkt" op drie plekken tegelijk:

| waar | hoe je het leest | wie schrijft het |
|---|---|---|
| de eigenschap op het element | `el.checked` | jij, en het component zelf |
| het `<input>` in de schaduwboom | `el.shadowRoot.querySelector('input').checked` | de browser bij een klik, het component bij een hertekening |
| de formulierwaarde | `new FormData(form).getAll(naam)` | het component, via `commitFormValue()` |

Ze lopen niet vanzelf gelijk. Wat je meet als "het vakje staat aan" hangt er dus van af
welke van de drie je toevallig aankijkt, en dat is precies waarom dit soort fouten stil is.

### Aanzetten vanuit code mag gewoon

`el.checked = true` werkt: het component ziet een echte wijziging, tekent zichzelf bij en
werkt zijn formulierwaarde bij. De dienstenkiezer selecteert zo zijn afhankelijkheden, en
de meting bevestigt dat het vakje, zijn `<input>` en de FormData daarna alle drie kloppen.

### Een klik TERUGDRAAIEN mag niet in dezelfde gebeurtenis

Hier zit de val. Bij een klik gebeurt dit, in deze volgorde:

1. de browser zet het `<input>` in de schaduwboom op `false`;
2. het component zet zijn eigen `checked` op `false` en plant een hertekening;
3. het component meldt de wijziging met een `change`-gebeurtenis;
4. onze handler draait.

Zet je in stap 4 `checked` terug op `true`, dan ziet de geplande hertekening dezelfde
waarde als de vorige keer en schrijft hij niets. Het `<input>` blijft dus op `false`
staan, terwijl het element zegt dat het aanstaat. Gemeten, met de schaduwboom erbij:

```
na de geweigerde klik : host checked=true,  eigen <input> checked=false
volgende klik         : <input> gaat naar true, host blijft true -> er gebeurt NIETS
de klik daarna        : weer gelijk, en pas dan werkt uitvinken
```

Op het scherm ziet dat eruit als: een vergrendelde dienst laat zich toch uitvinken, en
daarna reageert het vakje een klik lang niet. Zo is het ook gemeld.

**De regel: draai een geweigerde klik EEN TIK LATER terug.** Dan is de hertekening geweest
en is `false -> true` wel een echte wijziging, dus werkt het component zijn eigen `<input>`
en zijn formulierwaarde bij. In `static/js/wizard.js` heet dat `herstelVakje()`:

```js
function herstelVakje(svc, klaar) {
    setTimeout(function () {
        setChecked(svc, true);
        updateAllVisuals();
        klaar();
    }, 0);
}
```

De vlag die dubbele verwerking tegenhoudt (`processing`) blijft aanstaan tot ná dat
herstel, want het herstel veroorzaakt zelf weer een `change`.

Wat NIET werkt, en waarom het aantrekkelijk lijkt:

* `el.toggle()` aanroepen. Dat is de eigen weg van het component, maar in dezelfde
  gebeurtenis loopt hij tegen precies dezelfde hertekening aan. Gemeten: geen verschil.
* Het `<input>` in de schaduwboom rechtstreeks zetten. Dat werkt wel en het is precies wat
  je niet moet doen: je schrijft dan in de binnenkant van een component, en de eerstvolgende
  hertekening gooit het weer weg.

### Hoe je dit meet

Redeneren helpt hier niet, want alle drie de standen zijn plausibel. Meet ze naast elkaar,
in een echte browser:

```python
page.evaluate("""() => {
    const cb = document.querySelector('nldd-checkbox');
    const binnen = cb.shadowRoot && cb.shadowRoot.querySelector('input');
    const form = cb.closest('form');
    return {
        prop: cb.checked,
        attr: cb.getAttribute('checked'),
        binnen: binnen ? binnen.checked : null,
        formdata: form ? new FormData(form).getAll(cb.name) : [],
    };
}""")
```

Let op het verschil tussen een Playwright-selector en `page.evaluate`: een selector kijkt
WEL door schaduwbomen heen, `querySelector` in de pagina NIET. Een meting die op
`page.evaluate` met `querySelectorAll('input[name=...]')` leunt, meet bij deze componenten
niets en lijkt te slagen. Dat is een keer gebeurd: de test las een lege lijst en noemde dat
"niets verstuurd".

### De vangrail

`tests/e2e/test_locked_service_survives_submit.py` bevat de toets die hierop staat:
`test_na_een_geweigerde_klik_werkt_de_volgende_klik_meteen`. Hij faalt zonder het uitstel
en slaagt ermee; dat is gecontroleerd door de reparatie tijdelijk terug te draaien.

## De toets

`tests/e2e/test_aanvinkvakje.py` doet het hele rondje - aanvinken, opslaan, heropenen,
uitvinken, opslaan, heropenen - en toetst daarna het projectbestand zelf. Plus twee tests
op wat er over de lijn gaat, op beide vormen, want alleen daar is te zien dat het aan de
verzendkant zat.

Alles toetste tot nu toe AANzetten. Wat er misging was UITzetten.

`tests/forms/test_aanvinkvakje_stand.py` toetst de tekenkant en de opslagkant zonder
browser: welke stand het vakje krijgt bij een opgeslagen `false`, `true` en een afwezige
sleutel, dat er per element één id staat, en dat een schema dat gemarkeerd is dat na
opslaan nog steeds is (en een ongemarkeerd schema het niet wordt).

```bash
uv run pytest tests/e2e/test_aanvinkvakje.py -m "e2e and not sandbox" -q
uv run pytest tests/forms/test_aanvinkvakje_stand.py -q
```
