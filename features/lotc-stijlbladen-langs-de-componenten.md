# De stijlbladen langs de componenten

Per blok CSS één vraag: **bestaat hier een component voor in het NLDD-thema?** Zo ja, het
component gebruiken en de eigen regels weghalen. Zo nee, laten staan én in het bestand
opschrijven waarom, zodat de volgende niet opnieuw zoekt. Uitgevoerd in RC-74, als vervolg
op planpunt 3 van RC-70.

Het doel is niet nul eigen CSS. Het doel is dat er geen component wordt nagebouwd.

## Wat het opleverde

Vier stijlbladen zijn helemaal weg, de rest is geslonken. In totaal 1048 regels eruit,
393 erin (grotendeels toelichting).

| stijlblad | selectors voor | na |
|---|---|---|
| `project-details.css` | 192 | 156 |
| `wizard.css` | 140 | 105 |
| `dashboard.css` | 63 | 43 |
| `projects-overview.css` | 39 | 10 |
| `modal.css` | 21 | 19 |
| `metrics-charts.css` | 19 | 10 |
| `base.css` | 12 | 6 |
| `metrics-explorer.css` | 9 | 5 |
| `tools.css` | 4 | 3 |
| `lotc-app.css` | 4 | 4 |
| `admin-approvals.css` | 20 | 1 |
| `admin-users.css` | 11 | **weg** |
| `project-form-demo.css` | 7 | **weg** |
| `wizard-start.css` | 4 | **weg** |
| `admin.css` | 2 | **weg** |

## De afbeeldingen die steeds terugkomen

Deze patronen kwamen in bijna elk stijlblad terug. Wie een volgend eigen blok tegenkomt,
begint hier.

| eigen CSS | component |
|---|---|
| gekleurde pil met een status/soort erin | `<c-tag>`, waar het **type** de kleur bepaalt |
| kale `<table>` met eigen randen en kopregel | `<c-table>` + `c-table-head`/`c-th`/`c-table-row`/`c-td` |
| `display:flex` + `gap` | `<c-cluster>` |
| `display:flex` + `justify-content:space-between` | `<c-cluster justify="between">` |
| `repeat(auto-fit\|auto-fill, minmax(Npx, 1fr))` | `<c-auto-grid min="Npx">` |
| flexkolom met een gat | `<c-stack>` |
| wit vlak met padding, hoekstraal en schaduw | `<c-card>` |
| icoon + melding in een gekleurd vlak | `<c-alert type="...">` |
| eigen kruimelpad met `::after`-scheiding | `<c-breadcrumbs>` + `c-breadcrumbs-item` |
| genummerde stappenbalk met voortgangsbalk | `<c-step-indicator>` + `c-step-indicator-item` |
| rondje met initialen | `<c-avatar size="N">` |
| knoppenrij onder een formulier | `<c-action-group>` |
| handgebouwd label + `<select>` + hulptekst | `<c-select-field>` (met `native` als JS de lijst uitleest) |

Een tabel wordt onder NLDD een CSS-grid. **Zonder `columns=` stapelen de cellen**; de
kolombreedtes van de oude `.col-*`-klassen gaan daar als `fr`-waarden naartoe.

## De valkuil: een klasse die geen opmaak is

De opdracht waarschuwde ervoor en het ging in deze ronde tóch twee keer mis. Een klasse
kan een **haak** zijn: JavaScript of een test zoekt erop.

- `.edit-modal-close-btn` weggehaald → `test_lotc_confirmations.py` zoekt de sluitknop op
  als `nldd-button.edit-modal-close-btn` en liep in een timeout.
- `class="metrics-grid"` van de dashboardmeters gehaald → `test_lotc_pariteit.py` leest
  die meters uit met `locator(".metrics-grid")`.

**Grep de lezers voordat een klasse verdwijnt — en tests zijn ook lezers.** De oplossing is
niet het component laten schieten: zet het component neer en laat de klasse erop staan als
haak, met in het stijlblad de notitie dat er geen opmaak meer aan hangt.

Bekende haken: `config-item`, `config-code`, `copy-btn`, `deployment-section`, `is-hidden`,
`log-viewer-panel`, `service-cards-grid`, `edit-section-actions`, `edit-progress-actions`.

En omgekeerd: een klasse die nergens voluit in de markup staat is niet per se dood. Het
sjabloon stelt namen samen (`resource-color-` plus een volgnummer, `project-card-` plus de
toestand). Grep op de **prefix**.

## Waar een component níet paste

Alle vier gemeten in een browser, niet beredeneerd. Ze staan als verzoek in
`request_for_components.md` (11, 12, 13).

- **`<c-bar>`** belooft "links dit, rechts dat" maar rendert zijn middelste kolom alleen
  met een `center`-slot; zonder dat slot valt het end-slot in de MIDDELSTE kolom. Gebruik
  `<c-cluster justify="between">`.
- **`<c-avatar-group>`** rendert `<nldd-avatar-group>`, en dat element kent deze NLDD-bouw
  niet — het blijft `:not(:defined)` en doet stil niets.
- **`<c-code-viewer>`** neemt zijn tekst over bij het opbouwen. Een blok dat zijn inhoud
  pas later van JavaScript krijgt (`.textContent = ...`) blijft leeg. Om die reden houdt
  de toolspagina zijn eigen `<pre>` en het logpaneel zijn eigen regelbak.
- **`<c-modal-dialog>`** laat een eigen class niet door, en de gedeelde dialoog wordt
  volledig door `static/js/edit_modal.js` bestuurd via `#edit-section-modal`,
  `#edit-section-backdrop` en `.is-open`. Omzetten is een verhuizing naar de
  `show()`/`hide()`-API over tien sjablonen — een taak op zich, geen stijlbladronde.

## De dode ontwerpvariabelen

`static/css/lotc-app.css` vult onderaan de variabelen van het oude thema in. Namen die
daar niet staan leveren een **lege waarde**: een `gap` wordt geen ruimte, een achtergrond
geen kleur. De declaratie verdwijnt stil.

Er stonden er 24 op de aftellijst van `tests/test_css_dode_variabelen.py`. Die lijst is nu
**leeg**: bijna allemaal zaten ze op pillen (nu `<c-tag>`) en op kopbanden van tabellen (nu
`<c-table>`). Wat overbleef heeft een vaste waarde gekregen in plaats van een lege `var()`.

Let op de meetfout die hier makkelijk gemaakt wordt: tel niet álle `var(--rvo-*)`. De 30
namen in het shimblok lossen gewoon op — in de browser gemeten geeft `--rvo-space-md`
netjes `16px`. Alleen namen buiten dat lijstje zijn stuk.

## Hoe je dit meet

De poort is het **beeld**, niet de assertie: bij een omzetting verandert de markup per
definitie, dus een markuptest toetst alleen dát hij veranderd is.

```bash
uv run pytest tests/e2e/test_lotc_visual.py -o addopts="-p no:randomly -q" -m "e2e and not sandbox"
```

Die suite schrijft naar `tests/e2e/screenshots/lotc/` en faalt op componenten die het thema
niet implementeert (die renderen als zichtbare placeholder, niet als fout).

Twee dingen die tijdens deze ronde tot verkeerde conclusies leidden:

- Een preview-pagina zonder gegevens toont een lege tabel. Render het fragment met echte
  rijen voordat je zegt dat het klopt.
- Een probe die `host.innerHTML` VERVANGT sloopt de `<link>` naar het eigen stijlblad: die
  staat in de **body**, niet in de head. Vul aan in plaats van te vervangen — anders meet
  je een pagina zonder zijn eigen CSS en lijkt alles kapot.
