# De rvo-resten in de LOTC-templates

Wat er in `opi/templates_lotc/` nog aan het oude RVO-stijlsysteem herinnerde, wat daarvan
weg is, wat er bewust blijft staan en waarom. Gemeten en uitgevoerd in RC-62.

## Waarom dit iets is

De LOTC-omgeving laadt drie design systems:

```python
DESIGN_SYSTEMS = ["lotc-layout", "nldd", "lotc-forms"]   # opi/core/templates_lotc.py
```

`lotc_rvo` staat wel geinstalleerd en definieert 7568 verschillende `.rvo-`-selectors, maar
staat niet in die lijst en wordt dus niet geladen. Van de wel geladen pakketten definieert
alleen `lotc-forms` er drie (`.rvo-checkbox__group`, `.rvo-form-field__control`,
`.rvo-radio-button__group`) en die gebruiken onze templates nergens.

Een `rvo-`-klasse in een LOTC-template gaat daardoor mee naar de browser en doet daar
niets - tenzij een van ONZE eigen stylesheets hem opmaakt, en dat gebeurt vaker dan het
lijkt. Die twee gevallen vragen om een andere behandeling, en dat is de kern van deze
opruiming.

## Hoe je meet of een klasse iets doet

Een klasse is **dragend** als een stylesheet die op een LOTC-pagina geladen wordt hem
opmaakt, of als JavaScript hem opzoekt. De stylesheets die op een LOTC-pagina geladen
worden zijn: de geladen design systems, plus elke `static/css/*.css` die vanuit een
template met `static_url('css/...')` wordt ingeladen. Dat laatste is de valkuil: onder
ROOS komen die rvo-namen uit het echte RVO-stijlblad, maar wij hebben ze ook zelf
gedefinieerd in `wizard.css`, `metrics-explorer.css` en `project-form-demo.css`.

Van de 35 unieke rvo-klassen in de bereikbare templates (buiten
`architecture-overview.html.j2`) waren er elf dragend:

| klasse | reden |
|---|---|
| `rvo-breadcrumb` | `wizard.css`, `wizard-start.css` |
| `rvo-display-field__header` | `wizard.css` |
| `rvo-form-field__helper-group` | `wizard.css` |
| `rvo-sequence__items`, `__actions`, `__item-header`, `__item--inline` | `wizard.css` |
| `rvo-input`, `rvo-select` | `metrics-explorer.css` |
| `rvo-table` | `project-form-demo.css` |
| `rvo-form-field__error-text` | `wizard.js` (`scrollToFirstError`) |

Die zijn **hernoemd** naar `lotc-`, niet verwijderd. In de stylesheet staat de nieuwe naam
ERNAAST de oude in dezelfde regel:

```css
.rvo-breadcrumb,
.lotc-breadcrumb {
    ...
}
```

Bijschrijven en niet hernoemen, want `opi/templates/` (de roos-bouwlijn) gebruikt dezelfde
stylesheets met de rvo-naam. Zodra die boom verdwijnt kan de rvo-helft van elke regel weg.

De overige 24 klassen waren inert en zijn verdwenen. Bleef een `class`-attribuut daardoor
leeg achter, dan is het hele attribuut weg.

## Welke templates meetellen

Bereikbaar = een route rendert hem ooit. De invoerpunten zijn `render(lotc=...)`, de
directe `templates_lotc.TemplateResponse`-aanroepen, de `PREVIEWABLE_PAGES`- en
`REDESIGNED_PAGES`-allowlists in `opi/web/lotc_router.py`, **en de formulierwidgets**:

```python
# opi/forms/widgets/lotc.py
return self._env.get_template(f"widgets/{template_name}").render(...)
```

Die laatste is makkelijk te missen. De naam komt uit de velddefinitie, dus
`templates_lotc/widgets/*.j2` hangt aan geen enkele include - terwijl het de hele wizard
en elke bewerkdialoog is. Met die poort erbij: **171 bereikbaar, 41 niet.**

## Wat bewust blijft staan

### Het shimblok in `static/css/lotc-app.css`

Onderaan dat bestand staan 31 met de hand geschreven `--rvo-*`-definities. Dat is geen
restant maar een steunbalk: onze eigen stylesheets zijn tegen die namen geschreven en het
echte RVO-stijlblad wordt niet meer geladen.

Het plan bij deze opruiming wilde de variabelen omzetten en het blok daarna weghalen. Dat
kan niet, en dat is te meten: over de veertien stylesheets die vanuit `templates_lotc`
geladen worden staan **485 `var(--rvo-)`-verwijzingen**, en **alle 31** shimvariabelen
worden gebruikt (van 1x `--rvo-color-zwart` tot 81x `--rvo-space-md`). Het blok weghalen
maakt 485 declaraties leeg. De 87 verwijzingen in de templates zelf zijn maar het topje;
het werk zit in de stylesheets, en dat is de weg die het blok zelf al beschrijft: elke keer
dat een van onze stylesheets vervangen wordt door wat het thema levert, kan er een regel
weg.

### Waar wel en waar geen NLDD-tegenhanger voor is

Voor de **ruimtematen** bestaat een exacte tegenhanger, met dezelfde waarde:

| shim | NLDD |
|---|---|
| `--rvo-space-xs` (8px) | `--primitives-space-8` |
| `--rvo-space-sm` (12px) | `--primitives-space-12` |
| `--rvo-space-md` (16px) | `--primitives-space-16` |
| `--rvo-space-lg` (18px) | `--primitives-space-18` |
| `--rvo-space-xl` (24px) | `--primitives-space-24` |

Voor de **kleuren** niet, op vier na: `--rvo-color-hemelblauw`, `-donkerblauw`, `-groen` en
`-oranje` hebben in NLDD een `--primitives-color-reference-*` met exact dezelfde hexwaarde.
De hele grijstrap (`grijs-050` tot `grijs-900`, samen 106 verwijzingen), `--rvo-color-rood`
en alle tint- en schaduwstappen (`-150`, `-300`, `-600`, `-750`) hebben **geen** NLDD-token
met dezelfde waarde. Die omzetten is een ontwerpvraag - welke stap uit de NLDD-schaal hoort
bij onze "grijs-300"? - en geen zoek-en-vervang. Dat besluit staat open.

Wat wel al omgezet is: `widgets/button_group.html.j2` bouwde zijn tussenruimte op met
`var(--rvo-space-{{ gap }})` en gebruikt nu de NLDD-ruimtemaat. Zelfde pixels.

### `architecture-overview.html.j2`: vervangen, niet omzetten

Deze pagina draagt in haar eentje 158 van de klassen en 81 van de variabelen, in 1999
regels met 85 inline `style=`-attributen. De meting splitst hem netjes:

- de 158 klassen (18 unieke) zijn **allemaal inert** - geen geladen stylesheet maakt ze op,
  het eigen `<style>`-blok van de pagina ook niet;
- de 81 `var(--rvo-)`-verwijzingen zijn **wel levend**: die dragen via het shimblok de
  kleuren en afstanden van de pagina.

Het besluit is: **deze pagina wordt vervangen, niet omgezet.** De klassen eruit poetsen
verandert niets aan het scherm en niets aan het probleem; de variabelen kun je pas omzetten
als de grijstrap-vraag hierboven beantwoord is; en wat de pagina daarna nog is, is 85
inline styles in een enkel blok. Dat is een pagina om opnieuw te bouwen op componenten, en
dat is een eigen opdracht.

Tot dat gebeurt staat hij als uitzondering in `tests/test_lotc_geen_rvo_resten.py`, met
zijn aantallen erbij, zodat de uitzondering zichtbaar is en niet stilzwijgend.

### De 41 onbereikbare templates

Daar de klassen uithalen is poetsen aan iets wat niemand rendert. Of ze weg moeten is een
eigen besluit over dode templates. Ze staan hier op een lijst en zijn verder niet
aangeraakt; hun aantallen staan in de guard-test, zodat een van deze bestanden niet
stilletjes weer in gebruik genomen wordt met zijn rvo-klassen erin.

De zwaarste zijn `project-details/_argocd-deployment-card.html.j2` (27),
`wizard/partials/approval_items.html.j2` (13), `formulier-template.html.j2` (13),
`wizard/partials/backup_select_deployment.html.j2` (12), `roos-form-improved.html.j2` (12)
en `project-details/_resource-usage.html.j2` (12). De volledige lijst met aantallen staat
in `ONBEREIKBAAR_MET_KLASSEN` in de guard-test.

`templates_lotc/base.html.j2` stond ook in die lijst - een meegekopieerde ROOS-schil die
niets uitbreidde en die niemand renderde, met `body-class="rvo-theme rvo-responsive"` erin.
Die is weg; de echte schil is `base_lotc.html.j2`.

## Wat de opruiming niet raakt

**`opi/templates/`, de ROOS-boom.** Daar staan 603 voorkomens en die horen daar: dat is het
stijlsysteem dat die templates gebruiken. De schakelaar (`opi/web/lotc_switch.py`) houdt
beide wegen open met `?layout=roos`, en zolang die terugvalweg bestaat blijft die boom
staan zoals hij is.

**Commentaar.** Dat een blok uitlegt waar het vandaan komt met het woord `rvo-` is nuttig.
De opruiming gaat over attribuutwaarden en CSS-verwijzingen, niet over elk voorkomen van
vier letters. De guard-test knipt commentaar weg voor hij telt.

## De guard

`tests/test_lotc_geen_rvo_resten.py` telt per template de rvo-klassen in `class`-attributen
en de `var(--rvo-)`-verwijzingen, en legt ze naast een vastgelegde lijst. Hij faalt twee
kanten op:

- **meer dan vastgelegd** - er is een rvo-rest teruggekomen;
- **minder dan vastgelegd** - een uitzondering is overbodig geworden en hoort uit de lijst.

Die tweede richting is het punt: de lijst is een aftellijst en geen ontheffing.

## Hoe je zo'n opruiming toetst

Op de uitkomst en niet op de diff. Twee harnassen, allebei voor en na, met de
`class="..."`-attributen weggenormaliseerd:

1. **De echte routes.** `/lotc/`, `/lotc/formulier`, `/lotc/pagina/<slug>` voor alle
   `PREVIEWABLE_PAGES` en `/lotc/bg/<slug>` voor alle `REDESIGNED_PAGES`, met een
   `TestClient` op een kale FastAPI-app met alleen `lotc_router`. Dat zijn 73 pagina's.
2. **Elk bereikbaar template los**, met `undefined=ChainableUndefined` en een
   toegeeflijke stub voor `request`. Lussen over onbekende data leveren niets op, maar de
   vaste opmaak rendert wel - en die is precies wat een klassenopruiming raakt. Dat dekt
   de pagina's die echte projectdata nodig hebben (de hele `project-details/`-boom, de
   widgets): 165 van de 171.

Verschilt er buiten de class-attributen iets, dan is er meer geraakt dan de bedoeling was.
Let op de cache-bustende `?v=<hash>` achter `static_url()`: die verandert zodra je een
stylesheet aanraakt, en dat is geen bevinding.

## Twee dingen die de meting aan het licht bracht

- **`ButtonGroup.alignment` doet niets in LOTC.** `widgets/button_group.html.j2` zette de
  uitlijning met `rvo-action-group--{{ alignment }}`, en die klasse is inert. Dat stond dus
  al niet op het scherm; het is een gat in de omzetting en geen gevolg van de opruiming.
- **Het actieve tabblad op `project-details.html.j2` licht waarschijnlijk niet op.** De
  inline JavaScript zoekt het via `.rvo-tabs__item-link`, en die klasse komt in de
  LOTC-markup niet meer voor. Dat is een vraag over het tabs-component en geen
  klassenopruiming, dus die is blijven staan.
