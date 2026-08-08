# LOTC-bouwlijn: OPI op Lord of the Components

Een tweede uitvoering van de hele webinterface, gebouwd op **Lord of the Components**
(LOTC) met het NLDD-thema, naast de bestaande op `jinja-roos-components`. Bedoeld om te
kunnen beoordelen of LOTC jinja-roos kan vervangen, zonder de lopende release te raken.

Achtergrond en fasering: `plans/naar-het-nieuwe-componentensysteem.md`.
De samenleefmeting: `docs/lotc-samenleven-met-jinja-roos.md`.

## Wat het is

- Elke pagina bestaat twee keer. Het origineel op zijn eigen pad, de omgezette versie op
  `/lotc/pagina/<naam>`. Dat paar is het punt: los zegt een screenshot van de nieuwe
  versie niets over de vraag of de omzetting klopt.
- De navigatie volgt de opzet van [bg.rijks.app](https://bg.rijks.app/): hoofdnavigatie
  in een zijkolom met groepen, alleen hulplinks (account, in- en uitloggen) in de header.
- De formulierlaag is te bekijken op `/lotc/formulier`.

**Dit is een POC.** De release gaat voor. LOTC zit daarom in een eigen dependency-group
en niet in de runtime-dependencies, en alle nieuwe code zit achter een `ImportError`-
vangnet: in de release-image bestaat de bouwlijn niet.

## Gebruik

```bash
cd operations-manager/python
uv sync --group lotc          # of: uv sync --all-groups
uv run python scripts/lotc_convert_templates.py     # templates opnieuw omzetten
uv run pytest tests/test_lotc_conversion.py tests/test_lotc_icon_mapping.py -q
uv run pytest tests/e2e/test_lotc_visual.py -m "e2e and not sandbox" -q
```

De screenshots landen in `tests/e2e/screenshots/lotc/` (die map staat in `.gitignore`).

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
| `opi/templates_lotc_zad/` | componenten die ZAD zelf levert zolang LOTC ze niet heeft |
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

Wat LOTC nog niet heeft, staat in `opi/templates_lotc_zad/` en wordt na
`setup_components` in de registry gemerged onder een **eigen** owner-naam. Twee regels
houden dat opruimbaar:

- de **aanroep** is die van LOTC, zodat templates niet meeveranderen als hun versie komt;
- de **vormgeving** komt uit NLDD zelf (`nldd-button`, de settings-tokens), niet uit
  eigen kleuren.

Nu alleen `c-secret-field`. Opruimen is straks: de definitie weghalen en het template
verwijderen.

## Testen

| test | bewaakt |
|---|---|
| `tests/test_lotc_conversion.py` | dat elk template compileert, en dat de lijst uitzonderingen niet groeit |
| `tests/test_lotc_icon_mapping.py` | dat elke icoonvertaling naar een bestaande NLDD-naam wijst |
| `tests/e2e/test_lotc_visual.py` | dat pagina's in een browser kloppen, met screenshots |

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

152 van de 153 templates compileren; 17 van de 20 pagina's renderen zonder paginadata.

| open punt | bij wie |
|---|---|
| `c-data-list` onder NLDD (1 aanroep) - blokkeert het laatste template | LOTC |
| `c-secret-field` - wij draaien een eigen tijdelijke | LOTC |
| Lege keuzelijst bij `c-option`, `c-checkbox-field` zonder besturingselement - omweg in place | LOTC |
| 11 iconen zonder NLDD-tegenhanger; een onbekende naam rendert stil leeg | LOTC |
| `project-details`, `project-form-demo`, `wizard/wizard_page` renderen alleen met echte projectgegevens | ons |

De laatste rij is geen omzetprobleem: die pagina's compileren, maar de proefopstelling
heeft geen project. Ze zijn te zien op een draaiende instantie met data. Er is bewust
geen nepproject voor gemaakt - die context telt twintig sleutels uit services en
registry, en namaken levert een broos beeld op dat er echt uitziet.
