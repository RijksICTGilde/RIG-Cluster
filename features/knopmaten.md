# Knopmaten en knopvarianten

De regel voor `<c-button>`: welke maat een knop draagt, en welke `type` er bestaat.

## De regel

**Twee maten, en één daarvan schrijf je niet op.**

| maat | waarvoor | hoe je hem schrijft |
|---|---|---|
| `sm` | een knop in een dichte, herhaalde context: een rij in een tabel, een kaart in een lijst, de acties bij één item, een tijdvakkiezer boven een grafiek | `size="sm"` |
| `md` | alles daarbuiten: de hoofdactie van een pagina, de knoppen onder een formulier, de knoppen in een dialoog | **niets** -- `md` is de standaard van het component |

`md` niet opschrijven is de helft van de regel. Een maat die je op twee manieren kunt
schrijven wordt vanzelf op twee manieren geschreven, en dan is "heeft deze knop geen
maat of de standaardmaat?" niet meer te beantwoorden zonder het component te lezen.
Gemeten: `<c-button label="x"/>` en `<c-button size="md" label="x"/>` leveren exact
hetzelfde element op.

`xs` staat niet in de regel. Die maat stond op zes knoppen die in niets van hun buren
verschilden behalve dat ze kleiner waren; ze zijn `sm` geworden.

`lg` en `xl` bestaan wel in het thema maar horen niet bij een knop in dit portaal: een
knop die groter is dan de hoofdactie van de pagina heeft geen betekenis die de rest van
het scherm niet al draagt.

**De maat komt uit het component, nooit uit onze stylesheet.** Een eigen regel in
`static/css/` die een knop een andere hoogte geeft is de manier waarop dit uit elkaar
loopt: dan hangt de maat af van welk stijlblad die pagina toevallig laadt.

## De varianten

`type` op een `<c-button>` is de VARIANT, niet het HTML-type. Er zijn er zeven:

```
primary   secondary   tertiary   quaternary   subtle   warning   warning-subtle
```

Het HTML-type heet `html-type`. Een knop die een formulier indient is dus:

```jinja
<c-button html-type="submit" type="primary" label="Opslaan" />
```

`type="submit"` stond op negen knoppen en leverde twee fouten tegelijk op: onder het
NLDD-thema wordt onze `type` de `variant` van het element via een tabel die een onbekend
woord ongewijzigd doorgeeft, dus die knoppen kregen `variant="submit"` -- een variant die
geen enkel stijlblad kent -- en hun `html-type` bleef `button`, dus ze dienden hun
formulier niet in. Geen van beide gaf een melding.

## Een knop is een `<c-button>`

Niet een kale `<button>`. Die krijgt geen enkele klasse van het thema en staat er
onopgemaakt bij. Een `onclick` is geen reden om er een te schrijven: die gaat mee via
`:attrs`.

```jinja
{% set annuleren = "closeEditModal()" %}
<c-button type="secondary" label="Annuleren" :attrs="{'onclick': annuleren}" />
```

Twee uitzonderingen staan in de bewaker, met hun reden: het kopieerknopje IN een
kopieerveld en de ENV/YAML-schakelaar van het sleutel/waarde-veld. Dat zijn geen
paginaknoppen maar onderdelen van een veld, met eigen CSS en eigen JavaScript dat ze op
klasse terugvindt.

## De bewaker

`tests/test_lotc_knopmaten.py` leest de sjablonen zelf en valt over:

- een `size` die niet in de regel staat, of die uit een variabele komt (dan is hij niet
  te lezen, en kan hij stilletjes buiten de regel vallen);
- een `size` op een knop die NIET in een dichte context staat (zie hieronder);
- een `type` dat geen bestaande variant is;
- een kale `<button>` buiten de twee uitzonderingen.

### De omgekeerde toets

"Bestaat deze maat" is niet genoeg gebleken. `sm` bestaat, dus de hoofdacties van het
dashboard konden maandenlang kleiner zijn dan dezelfde knop op elke andere pagina met
alle toetsen groen; het is drie keer door een gebruiker gemeld en nooit door een test.

Wat wél machinaal te zien is: een knop op een **paginasjabloon** (een die een layout
uitbreidt -- dat is het hele scherm, en daar staan de hoofdacties) die **niet in een
`{% for %}`** staat, is geen rij in een tabel en geen kaart in een raster. Zo'n knop
is verdacht en moet zijn reden opschrijven in `SM_OP_EEN_PAGINA` in de bewaker, met
`(sjabloon, label)` als sleutel -- een regelnummer verschuift bij de eerste bewerking.

De fragmenten (partials, modals, kaarten) blijven buiten deze toets: die zijn de dichte
context zelf, want ze worden per item of binnen een dialoog gerenderd.

De lijst telt nu zes regels, allemaal een knop in de kopregel van een paneel of in een
kaart. Wordt hij lang, dan is dat het signaal dat de regel zelf niet meer klopt -- niet
dat er een regel bij moet.

Waar de variant uit Python komt in plaats van uit het sjabloon -- `DeploymentAction.kind`
en `ProjectAction.kind`, die rechtstreeks het `type`-attribuut worden -- struikelt de
dataclass er zelf over, via `check_button_variant` in `opi/core/buttons.py`. Dat is de
enige plek waar de twee vocabulaires staan; de test en de dataclasses lezen allebei
daaruit.

```bash
uv run pytest tests/test_lotc_knopmaten.py -q
```
