# LOTC-bouwlijn: OPI op Lord of the Components

De omzetting van de webinterface naar **Lord of the Components** (LOTC) met het
NLDD-thema, ter vervanging van `jinja-roos-components`. Hij gaat pagina voor pagina: een
route kiest met `?ui=lotc` welke weergave hij rendert, en zodra een pagina af is
verdwijnt die keuze.

Achtergrond en fasering: `plans/naar-het-nieuwe-componentensysteem.md`.
De samenleefmeting: `docs/lotc-samenleven-met-jinja-roos.md`.

## Wat het is

- **Echte routes** die hun pagina al door LOTC kunnen renderen: `/services`,
  `/dashboard`, `/projects` en `/projects/details/<naam>` (met het resourcegebruik-
  fragment dat htmx apart inlaadt), plus de beheerpagina's `/admin/users` (inclusief het
  formulier op `/admin/users/create` en `/admin/users/<id>/edit`), `/admin/approvals` en
  `/admin/usage`, en **de wizard** (`/forms/wizard/start`, `/forms/wizard/<flow>`,
  `/forms/wizard/<flow>/edit/<project>`, elke htmx-stap en de samenvatting). Zet er
  `?ui=lotc` achter.
- Daarnaast een **proefopstelling** onder `/lotc/`, met voorbeeldprojecten. Die is er om
  vorm te kiezen zonder een cluster nodig te hebben, niet als eindbestemming.
- De navigatie volgt de opzet van [bg.rijks.app](https://bg.rijks.app/): hoofdnavigatie
  in een zijkolom met groepen, alleen hulplinks (account, in- en uitloggen) in de header.
- De formulierlaag is te bekijken op `/lotc/formulier`.

**De omzetting gaat pagina voor pagina.** Lord of the Components is een gewone
runtime-dependency: de applicatie rendert er pagina's mee, dus hij hoort in de image. Een
route kiest met `?ui=lotc` welke weergave hij rendert; zonder die vlag blijft de
bestaande pagina onveranderd. Zodra een pagina af is verdwijnt die keuze, en met de
laatste pagina verdwijnt de schakelaar zelf.

## Gebruik

```bash
cd operations-manager/python
uv sync
uv run python scripts/lotc_convert_templates.py     # templates opnieuw omzetten
uv run pytest tests/test_lotc_conversion.py tests/test_lotc_icon_mapping.py -q
uv run pytest tests/e2e/test_lotc_visual.py -m "e2e and not sandbox" -q
```

De screenshots landen in `tests/e2e/screenshots/lotc/` en staan in de repo: ze zijn het
resultaat waar de omzetting op beoordeeld wordt, niet een wegwerpartefact.

Draaiende applicatie:

| pad | wat |
|---|---|
| `/lotc/` | de schil, zonder pagina-inhoud |
| `/lotc/pagina/<naam>` | een omgezette pagina, bijvoorbeeld `dashboard` |
| `/lotc/formulier` | elk veldtype, uit voorbeeldvelden |
| `/static/lotc/...` | de CSS en JS van de actieve design systems |

## Hoe het in elkaar zit

| bestand | rol |
|---|---|
| `opi/core/templates_lotc.py` | de tweede Jinja-omgeving, met design systems, globals en filters |
| `opi/templates_lotc/` | de omgezette templates - **gegenereerd**, niet met de hand bewerken |
| `opi/templates_lotc/base_lotc.html.j2` | de schil in bg-opzet - **wel** met de hand |
| `opi/templates_lotc/widgets/` | de formulierwidgets - **wel** met de hand |
| `opi/templates_lotc/bg/` | de hertekende pagina's - **wel** met de hand |
| `opi/templates_lotc/bg/_patterns.html.j2` | gedeelde patronen: `panel()`, `page_head()`, `info()`, `service_card()` |
| `opi/templates_lotc/bg/wizard-page.html.j2`, `wizard-start.html.j2`, `_wizard-step.html.j2`, `_wizard-steps.html.j2`, `_wizard-review.html.j2` | de wizard: de hele pagina, de startpagina, en de drie fragmenten die htmx wisselt |
| `opi/web/lotc_switch.py` | de schakelaar waarmee een echte route zijn weergave kiest |
| `opi/web/lotc_fixtures/` | voorbeeldprojecten voor de proefopstelling |
| `opi/web/navigation_lotc.py` | de indeling van de navigatie en de icoonvertaling |
| `opi/web/lotc_router.py` | de routes onder `/lotc/` |
| `opi/forms/widgets/lotc.py` | de widget-adapter die de LOTC-templates rendert |
| `opi/forms/lotc_attrs.py` | de attribuutbundel van een veld, voor LOTC's `:attrs` |
| `scripts/lotc_convert_templates.py` | de omzetter |

### Twee omgevingen, geen twee design systems

Beide componentsystemen registreren een Jinja-extensie die de bron voorbewerkt en **elke**
`<c-*>`-tag opeist. In één `Environment` claimt de eerst geregistreerde voorbewerker alles
en breekt op de tags die hij niet kent; een doorlaatstand bestaat aan geen van beide
kanten. Twee losse omgevingen werken wel.

De grens loopt daarbij niet per pagina maar **per overervingsketen**: een template dat
`base_lotc.html.j2` uitbreidt wordt door de LOTC-omgeving gerenderd, een template dat
`base.html.j2` uitbreidt door de roos-omgeving. Mengen binnen een keten kan niet.

### Een omzetter, geen handwerk

De release blijft aan diezelfde templates werken. Een met de hand overgetypte kopie van
152 bestanden is vanaf dag twee verouderd zonder dat iemand ziet waar. Daarom genereert
`scripts/lotc_convert_templates.py` de inhoud van `opi/templates_lotc/`, en zit het
handwerk in de vertaalregels.

Twee soorten templates vallen daarbuiten en staan er met de hand, omdat er een keuze aan
te pas komt die een script niet kan maken: de schil (de indeling verandert, niet alleen
de namen) en de formulierwidgets (welk attribuut waar hoort, per widget).

Wijzig je iets in `opi/templates/`, draai dan de omzetter opnieuw.

### Wat er anders is aan LOTC

Bij het omzetten komen steeds dezelfde vier dingen terug:

1. **Attributen zijn kebab-case** (`body-class`, `max-width`), niet camelCase.
2. **Samenstellingen krijgen kinderen, geen data-props.** `<c-menu>` met `<c-menu-item>`
   in plaats van `:items="..."`.
3. **Jinja mag niet op attribuutpositie.** Waar roos-templates een macro een stuk
   attribuut-tekst midden in de tag lieten schrijven, gebruikt LOTC `:prop="expr"` -
   `none` betekent weglaten - en `:attrs="<dict>"` voor een hele bundel.
4. **`<c-page>` bedraadt de `<head>` zelf**, inclusief de CSS en JS van elk actief design
   system onder `/static/lotc/`.

### De volgorde van de design systems ligt vast

```python
DESIGN_SYSTEMS = ["lotc-layout", "nldd", "lotc-forms"]
```

`lotc-forms` hoort achteraan, ná het visuele thema, anders lossen de invoervelden niet
op. `lotc-layout` is formeel opt-in maar in de praktijk vereist: de structuurprimitieven
renderen uitsluitend via dat pakket.

### Componenten die wij zelf leveren

Nu geen. Er stond een eigen `c-secret-field` omdat LOTC die niet had; sinds hun versie er
is, is die weg. De opruiming was wat we ervan hoopten: het registratieblok weg, het
template weg, geen paginawijziging - de aanroepvorm was al die van hen.

Komt het opnieuw voor, dan is de weg: `merge_fragment` op de registry, plus de eigen
templatemap op de `searchpath`. Twee regels, en twee regels om weer weg te halen.

## Testen

| test | bewaakt |
|---|---|
| `tests/test_lotc_conversion.py` | dat elk template compileert, en dat de lijst uitzonderingen niet groeit |
| `tests/test_lotc_icon_mapping.py` | dat elke icoonvertaling naar een bestaande NLDD-naam wijst |
| `tests/e2e/test_lotc_visual.py` | dat pagina's in een browser kloppen, met screenshots |
| `tests/test_lotc_layout_rules.py` | dat kaarten via `panel()` gebouwd worden en gaps uit de schaal komen |
| `tests/test_lotc_schrijfwijze.py` | dat teksten de lezer met "je" aanspreken |
| `tests/test_lotc_switch.py` | dat `?ui=lotc` alleen op die exacte waarde aanslaat |

Compileren is een echte poort en geen telling: LOTC valideert bij het compileren al of
elk component bestaat en of elk attribuut bij dat component hoort.

**De screenshottests zijn niet optioneel.** Twee fouten in de formulierlaag zagen er in
de HTML goed uit en waren in de browser stuk: een keuzelijst zonder opties (een browser
gooit binnen een `<select>` alles weg wat geen `<option>` is) en een aanvinkvakje zonder
besturingselement. Geen compileertest of HTML-assertie ving die.

Ze wachten expliciet tot de NLDD-webcomponenten door de browser zijn opgebouwd
(`*:not(:defined)`); daarvoor leg je ongestileerde tekst vast.

Er staat bewust **geen** pixelvergelijking met een baseline op. Zolang de omzetting loopt
verandert het beeld elke stap, en dan wordt een baseline elke stap opnieuw goedgekeurd.
Komt die er, dan hoort erbij: een gepinde `device_scale_factor`, vastleggen en vergelijken
in dezelfde container-image, en een drempel in plaats van een exacte match.

## Stand en wat er open staat

Alle 164 templates compileren. Vier echte routes kunnen hun pagina door LOTC renderen.

| open punt | bij wie |
|---|---|
| De wizard: de e2e-gedragstests zijn op de roos-markup geschreven | ons |
| `/admin/*`, `metrics-explorer`, `about` aansluiten | ons |
| `architecture` - 1509 regels in een blok; verdient een eigen besluit | ons |
| Iconen: de NLDD-woordenschat telt er 60, de RVO-set die roos meelevert 1163. Voorstel om die als losse implementatiemodule mee te nemen ligt bij LOTC | LOTC |

### Een aandachtspunt voor de bouw

LOTC komt van `git.claude.robbertuittenbroek.nl`. Dat werkt hier, maar een image die in
een andere omgeving gebouwd wordt moet die host kunnen bereiken. `jinja-roos-components`
komt van GitHub, dus een git-dependency is niet nieuw; deze host is dat wel.
