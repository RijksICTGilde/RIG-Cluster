# ROOS eruit

`jinja-roos-components` is uit de Operations Manager verdwenen. Er wordt overgestapt op
Lord of the Components (LOTC) met het NLDD-thema, niet parallel gedraaid: elke pagina,
elk fragment en elke dialoog rendert nog in EEN componentensysteem.

Achtergrond en fasering: `plans/roos-eruit.md` en `features/lotc-bouwlijn.md`.

## Wat er weg is

| | |
|---|---|
| `opi/templates/` | 155 sjablonen, de hele roos-boom |
| de schakelaar | `chosen_layout()`, `wants_lotc()`, `DEFAULT_LAYOUT`, `?layout=`, het koekje `zad_layout` |
| de roos-omgeving | `opi/core/templates.py` heet nu `opi/core/template_helpers.py` en draagt alleen nog filters, paden en `static_url()` |
| de afhankelijkheid | `jinja-roos-components` uit `pyproject.toml` en `uv.lock` |
| de assets | de mount `/static/roos/dist`, en `unpkg.com` uit de CSP (daar kwam de HTMX van roos vandaan; die komt uit `static/js/`) |
| de tweede adapter | `opi/forms/widgets/roos.py` is `fields.py` geworden: alleen nog de gedeelde veldvoorbereiding, zonder eigen Jinja-omgeving |
| de dubbele dienstsjablonen | elke dienst had `section-x.html.j2` en `section-x-lotc.html.j2`; er is er nog een, en `lotc_counterpart()` is daarmee overbodig |
| de omzetter | `scripts/lotc_convert_templates.py` en `scripts/lotc_coexistence_probe.py` hadden geen invoer meer |

`opi/web/lotc_switch.py` bestaat nog. Wat erin zit is `render()`/`render_fragment()` -
een gewone render van een sjabloon uit `opi/templates_lotc/` - plus de functies die de
gegevens van een route in de vorm zetten die de hertekende pagina leest, en de
weergavekeuze licht/donker.

## Wat de pariteitspoort vervangt

De omzetting is niet gelukt door goed kijken maar door meten:
`tests/e2e/test_lotc_parity.py` en `tests/e2e/test_lotc_modal_pariteit.py` haalden elke
route twee keer op (`?layout=roos` en `?layout=nldd`) en legden het gedragsoppervlak naast
elkaar. Zo zijn een keuzelijst, een knop en de velden van een filter teruggevonden die
stilzwijgend verdwenen waren - geen van drieen gaf een foutmelding.

Met de oude pagina weg meet die vergelijking de ene helft van niets tegen de andere. Wat
ervoor in de plaats staat is dezelfde meting met een **vastgelegde lijst** als bron:

| bestand | wat |
|---|---|
| `tests/oppervlak.py` | de meetlat: bestemmingen, htmx-adressen, JavaScript-aanroepen, velden met een naam, id's |
| `tests/oppervlak_snapshot.json` | wat elke pagina, elk tabblad en elk dialoogfragment draagt (39 paden) |
| `tests/e2e/test_gedragsoppervlak.py` | de poort erop: faalt als er iets van die lijst VERDWIJNT |
| `tests/oppervlak_snapshot_fragmenten.json` | hetzelfde voor de vijf dialoogfragmenten die zonder takendienst niet via HTTP te bereiken zijn |
| `tests/test_lotc_modal_fragmenten.py` | de poort daarop |

Iets ERBIJ is gewoon nieuw werk en faalt niet; iets dat WEG is, is bijna altijd een
ongeluk.

De lijst bijwerken:

```bash
ZAD_SCHRIJF_OPPERVLAK=1 uv run pytest tests/e2e/test_gedragsoppervlak.py -m e2e
ZAD_SCHRIJF_OPPERVLAK=1 uv run pytest tests/test_lotc_modal_fragmenten.py
```

Lees die diff dan ook echt. Elke regel die verdwijnt is gedrag dat verdwijnt, en dat is
precies wat deze poort hoort te vangen; hoort het weg te zijn, zet de reden dan in de PR.

Dit is minder elegant dan een vergelijking en het veroudert. Het is wel eerlijk: eerst was
de oude pagina de norm, nu is de nieuwe het.

## Wat er onderweg boven kwam

Een sloop van deze omvang legt bloot wat de omzetting stil had laten vallen. Vijf dingen,
allemaal groen in de suite en onzichtbaar in de HTML-bron:

1. **"N wijzigingen nog niet uitgerold"** stond niet op de hertekende projectpagina. De
   omzetter maakte er wel een sjabloon van, maar geen pagina nam het op, en de knop erin
   had zijn aanroep verloren. Nu `bg/_pending-rollout.html.j2`, boven de tabbalk.
2. **De statuschip van een geheim veld was leeg**: het omgezette `display_card`-sjabloon
   liet het label van de chip vallen, dus "Versleuteld opgeslagen" stond er niet meer.
3. **Het menu-icoon van Uitloggen** had geen NLDD-tegenhanger en rendeerde leeg. De
   icoontoets meet nu ook het menu; hij deed dat eerder tegen de SVG-bestanden van
   jinja-roos, en die zijn er niet meer.
4. **De uitleg van een dienst** kreeg zijn icoonnaam niet door de vertaling heen, dus elke
   hulpdialoog toonde een leeg icoon boven zijn titel.
5. **Twee tests maten een sjabloon dat geen enkele route rendert** - de eerste automatische
   omzetting, die naast de handgeschreven `bg/`-versie was blijven liggen.

## Waar op te letten

**Een sjabloon dat niemand rendert is niet automatisch dood.** `opi/templates_lotc/`
bevat naast de handgeschreven `bg/`-pagina's nog de eerste automatische omzetting van de
oude boom. Die hangt aan `/lotc/pagina/<naam>` en aan een paar tests, en is NIET wat een
gebruiker ziet. Meet je iets, meet het dan op wat de route rendert.

**De klassen die geen vormgeving zijn blijven.** `config-item`, `config-code`, `copy-btn`,
`deployment-section`, `is-hidden`: daar hangt JavaScript aan. Ze zien eruit als opmaak en
zijn het niet.

**`opi/templates_lotc/` is nu met de hand beheerd.** De omzetter die die map genereerde is
weg met zijn invoer.
