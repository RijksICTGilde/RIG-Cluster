# De diensten in het nieuwe thema (RC-65)

> **RC-67: dit stuk beschrijft de tijd dat er TWEE componentsystemen waren.** Roos is weg,
> en daarmee de `-lotc`-tegenhangers en `lotc_counterpart()`: elke dienst heeft nog EEN
> sjabloon, onder zijn eigen naam. Wat hier staat is de weg ernaartoe en de reden dat het
> zo gelopen is; de eindstand staat in `features/roos-eruit.md`.


De nieuwe vormgeving is de standaard (`DEFAULT_LAYOUT = LAYOUT_LOTC`), maar de blokken die
een **dienst** levert vielen nog terug op de oude. Dat is nu voor elk dienstsjabloon
opgelost, en de brug ertussen (`render_roos()`) is weg.

## Wat een dienst nu levert

Naast elk sjabloon in `opi/services/catalog/<dienst>/` ligt zijn LOTC-tegenhanger, met
`-lotc.html.j2` als achtervoegsel:

```
opi/services/catalog/shared/
  section-backups.html.j2          # roos-componenten
  section-backups-lotc.html.j2     # LOTC-componenten
  _job-modal.html.j2
  _job-modal-lotc.html.j2
  ...
```

`lotc_counterpart(naam)` (in `opi/core/templates_lotc.py`) zoekt de tegenhanger op naam.
Een afspraak en geen tabel: een nieuwe dienst hoeft niets bij te werken buiten zijn eigen
map. Dat is de regel uit RC-36 — een dienst draagt alles wat hij is in zijn eigen map.

Vier tegenhangers stonden hiervoor in `opi/templates_lotc/bg/`, aangewezen door een
handgeschreven tabel in `_deployment-service-sections.html.j2`. Die tabel is weg: hij was
handwerk op de verkeerde plek, en een dienst die hem vergat viel stilzwijgend terug op de
roos-weergave.

## Twee dingen die je hier makkelijk fout doet

**Op `c-button` is `type` de VORMGEVING.** `type="primary"` is de stijl; het
HTML-attribuut heet `html-type`, met `button` als standaard. Een knop met
`:attrs="{'type': 'submit'}"` ziet er goed uit en doet niets.

**Een `<nldd-button>` dient een omliggende `<form>` niet in.** Ook `html-type="submit"`
helpt niet, en zelfs `form.requestSubmit()` levert geen verzoek op. Geef de knop zelf een
`hx-post`, zoals de databaseconsole dat in beide vormgevingen al doet.

**Een NLDD-veld is form-associated.** Zijn waarde komt wel in de `FormData` van een
omliggend formulier, maar het echte `<input>` zit in de shadow root en is onzichtbaar voor
de DOM-query die htmx voor `hx-include` doet. Laat `hx-include` daarom op de VELDEN wijzen
(`#job-form-x nldd-text-field`) en niet alleen op de wikkel — anders vertrekt het verzoek
zonder de ingevulde waarden.

## Hoe dit bewaakt wordt

| Wat | Waar |
|---|---|
| Elk sjabloon in de catalogus heeft zijn tegenhanger | `tests/test_lotc_dienstblokken.py` |
| De twee doen hetzelfde (bestemmingen, htmx, JS, id's) | idem, per dienstblok en per dialoogtoestand |
| Er komt geen roos-HTML uit een LOTC-blok | idem |
| De dialoog kan nog VERSTUREN | idem |
| De knoppen vuren echt af, met de ingevulde waarden | `tests/e2e/test_lotc_dienstdialogen.py` |
| Geen enkele interne link levert een 404 | `tests/e2e/test_lotc_links.py` |
| Elk scherm met beeld nagelopen | `scripts/lotc_visuele_sweep.py` |

De eerste poort keek alleen naar `*/section-detail.html.j2`, en dat was te smal: het
deploymentblok van metrics_scraper, het backupblok en de twee dialogen vielen erbuiten.
Hij telt nu elk `.html.j2` in de catalogus.

## De visuele sweep

```bash
uv run python scripts/lotc_visuele_sweep.py \
    --base https://zad.sandbox.rijksapp.dev \
    --secret <SECRET_KEY van de draaiende app> \
    --email <adres op de allowlist> \
    --uit /tmp/sweep
```

Bezoekt elke pagina, elk projecttabblad en elke dialoog, maakt er schermafbeeldingen van en
meet wat je op een plaatje wel ziet maar in de markup niet: roos-HTML in het antwoord, lege
iconen, een berekende `gap` van nul, consolefouten en mislukte verzoeken.

Twee dingen die het script over zichzelf heeft geleerd, en die het nu bewaakt:

- **Meet niet op de toegangspagina.** Met een e-mailadres dat niet op de allowlist staat
  levert elke route dezelfde nette "Geen Toegang"-pagina, en dan meldt de sweep opgewekt
  nul bevindingen over elf schermen die niemand gezien heeft.
- **Open een dialoog IN zijn pagina.** Een fragment rechtstreeks bezoeken levert HTML
  zonder `<head>`: geen stijlbladen, geen web-componenten. Elke `gap` is dan nul en elk
  icoon nul breed — dat gaf zeven bevindingen die geen van alle bestonden.
