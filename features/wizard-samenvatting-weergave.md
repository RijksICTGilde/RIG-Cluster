# Wat een samenvattingsscherm laat zien

De reviewstap van de wizard en de samenvatting in de bewerk-modal tonen wat er is
ingevuld. Niet elk veld hoort daar letterlijk te staan: een geheim heeft wél een
waarde, en juist die waarde is het probleem. Dit document beschrijft hoe een veld
zelf bepaalt hoe het in een samenvatting verschijnt.

## Wat het is

Een editable kan een `summarizer` declareren. Die krijgt de waarde en geeft terug
wat er moet staan, of `None` om het veld helemaal weg te laten uit de samenvatting.

```python
from opi.forms.editables.summarizers import HiddenSummary, MaskedSummary

WEBHOOK_TOKEN_EDITABLE = Editable(
    yaml_path="components[*]/webhook-token",
    summarizer=HiddenSummary(),      # staat niet in de samenvatting
)

DEPLOY_KEY_EDITABLE = Editable(
    yaml_path="components[*]/deploy-key",
    summarizer=MaskedSummary(),      # "Ingesteld", zonder de waarde
)
```

`MaskedSummary` neemt een eigen tekst (`MaskedSummary(text="Geconfigureerd")`) en
kan met `empty_text` ook iets tonen als het veld leeg is; standaard valt een leeg
veld weg, want een veld dat niemand invulde is ruis.

Een eigen implementatie is elke klasse met deze methode:

```python
def summarize(self, value: Any, context_data: dict[str, Any] | None = None) -> str | None
```

`None` betekent "toon dit veld niet". `context_data` is het omringende
projectbestand, voor het geval de samenvatting van een ander veld afhangt.

## Waarom naast `converter.view()`

`EditableConverter` heeft al `read` (YAML -> formulier), `write` (formulier ->
YAML) en `view` (YAML -> alleen-lezen weergave), en `view` wordt in de samenvatting
gebruikt: `EncryptedDisplayConverter` maakt er "Versleuteld opgeslagen" van. Dat
blijft zo. Een `summarizer` bestaat ernaast om twee redenen:

- `view()` kan niet zeggen "toon niets". Geef je `None` terug, dan belandt die in
  `str()` verderop en staat er letterlijk het woord `None` op het scherm.
- `view()` hangt aan de converter, en een converter gaat óók over opslag. Een veld
  dat alleen zijn samenvatting wil regelen moest daarvoor een converter verzinnen
  die verder niets doet.

Staat er allebei iets, dan wint de `summarizer`: op een samenvattingsscherm is dat
de specifiekere uitspraak.

## Waarom op de `Editable` en niet op de visualizer

Of iets een geheim is, is een eigenschap van het gegeven, niet van dit ene scherm.
Op de `Editable` geldt de declaratie in elke flow die dat veld hergebruikt, ook een
flow die later wordt geschreven. Dat is dezelfde reden waarom API-velden de gedeelde
editables hergebruiken in plaats van een eigen validatie te krijgen.

## Waar het geldt

Er zijn twee samenvattingbouwers en ze gaan allebei door `_format_value` in
`opi/web/router_wizard.py`:

| Bouwer | Gebruikt door |
|---|---|
| `_build_section_summary` | de reviewpagina van de volledige wizard (HTML-string) |
| `_build_section_fields` | de bewerk-modal (gestructureerd, template rendert) |

Ook binnen een sequence (componenten, gebruikers) en één niveau dieper (een
sequence in een sequence) geldt de declaratie. Dat laatste is een aandachtspunt bij
wijzigingen: die tak formatteerde zijn waarden vroeger zelf en sloeg `_format_value`
over, waardoor een verborgen veld daar alsnog verscheen.

Een `summary_fn` op een `FormSection` bouwt de hele samenvatting van die stap zelf
en gaat dus buiten dit mechanisme om. Zet je die, dan ben je zelf verantwoordelijk
voor wat er in staat én voor het escapen ervan.

## Twee vangnetten die blijven staan

- **Key-value velden** (aliassen, eigen omgevingsvariabelen) worden nooit uitgeschreven
  in een samenvatting, op widgettype. Dat blijft naast de `summarizer` bestaan, zodat
  een key-value veld dat later wordt toegevoegd gedekt is zonder dat iemand eraan hoeft
  te denken.
- **Escapen.** De reviewpagina rendert de samenvatting met `| safe`, dus alles wat
  tussen de tags belandt gaat eerst door `_summary_text()`. Waarden omdat iemand ze
  intypte, labels omdat het niets kost en een label dat ooit dynamisch wordt dan al
  gedekt is. Nooit toepassen op geneste fragmenten die deze module zelf bouwde --
  dan zie je de tags op het scherm.

## Tests

`tests/test_wizard_summary_display.py` pint beide kanten: dat een verborgen veld
verborgen blijft (ook in een sequence, ook een niveau dieper, en dat een item waarvan
alles verborgen is niet alsnog rauw wordt gedumpt) en dat getoonde waarden geëscaped
zijn.
