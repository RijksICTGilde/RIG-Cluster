# De donkere weergave is nauwelijks te lezen: ligt dat aan ons of aan de componenten?

Goedgekeurd plan voor RC-134 (PR #132). De uitwerking en de metingen staan in
`features/donkere-weergave-contrast.md`; het antwoord aan de componentenlaag in
`request_for_components.md`.

In donkere modus staat er op meerdere schermen grijze tekst op een donkergrijze achtergrond, zo flauw dat labels en waarden bijna wegvallen. Gezien op het cross-domain-blok van een projectpagina (labels als "Bron-project" en hun waarden) en op het voortgangsscherm van een taak (de stappenlijst en de kop "Voortgang").

De vraag van de opdrachtgever is de goede eerste vraag: **ligt dit aan ons of aan de componentenlaag?** Beantwoord dat eerst en met een meting, want het bepaalt of de reparatie hier hoort of in `request_for_components.md`.

## Waar te beginnen

**Meet de werkelijke kleuren, niet de indruk.** Neem in de browser in donkere modus voor een paar van die onleesbare elementen de berekende `color` en de `background-color` van hun achtergrond, en reken het contrast uit (WCAG AA vraagt 4,5:1 voor gewone tekst, 3:1 voor grote tekst). Doe hetzelfde in lichte modus. Nu heb je cijfers in plaats van "het oogt raar", en zie je meteen of het om enkele elementen gaat of om alles.

**Bepaal daarna waar de kleur vandaan komt.** Per onleesbaar element: staat de kleur op een NLDD-component (dan is het de componentenlaag), of komt hij uit een van onze eigen keuzes? Twee plekken waar wij het zelf kunnen veroorzaken, allebei gemeten:

- **Vaste kleurnamen in onze sjablonen.** `grep -o 'color="[^"]*"' opi/templates_lotc/` geeft 23 treffers met een vaste toonnaam, waarvan `grijs-050` twaalf keer. Een grijstint die op wit werkt, werkt op donker niet: `grijs-050` is bijna wit, en daar is een donkere achtergrond niet het probleem, maar `grijs-500` en `grijs-600` zijn dat wel. Zoek uit of die namen in het thema tokens zijn die meebewegen met de modus, of vaste waarden.
- **Vaste hexkleuren in onze eigen CSS.** In `static/css/*.css` staan 47 regels met een hardgecodeerde `color:` of `background:`. Die bewegen per definitie niet mee met een modus. Zoek uit welke daarvan op de getroffen schermen worden geraakt: `admin-approvals.css`, `dashboard.css`, `project-details.css`, `modal.css` en `metrics-*.css` zijn de kandidaten.

**Kijk ook hoe de modus wordt gezet.** `base_lotc.html.j2:49` leest een koekje `zad_scheme`. Zoek uit hoe die keuze bij de componenten terechtkomt (een attribuut op de wortel, een klasse, of `prefers-color-scheme`), en of onze eigen CSS diezelfde schakelaar volgt. Een tweede mechanisme naast dat van het thema is een waarschijnlijke bron van precies dit beeld: het thema gaat om, onze eigen regels niet.

## Het oordeel dat deze taak moet opleveren

Per getroffen plek één van drie, met de meting eronder:

1. **Van ons**: een vaste kleur in onze sjablonen of CSS. Repareren door de themawaarde te gebruiken in plaats van een vaste, en niet door er een tweede vaste kleur voor donker naast te zetten.
2. **Van de componentenlaag**: het component levert in donkere modus een onvoldoende contrasterende combinatie. Dan hoort het met de meting in `request_for_components.md`, zoals eerder bij `width=` en de toolbar-overloop, en beschrijf wat wij intussen doen.
3. **Van de combinatie**: wij zetten een component in een context waarin zijn kleuren niet meer kloppen, bijvoorbeeld een gedempte tekstkleur op een achtergrond waar het component niet op rekent. Dan is de reparatie onze keuze van context.

## Wat er buiten valt

- Een eigen donker thema bouwen. Als het thema het niet goed doet, is dat een melding aan de componentenlaag, geen reden om er een tweede kleurenstelsel naast te zetten.
- De lichte modus, tenzij dezelfde reparatie hem raakt; meld dat dan.

## Verifieerbaar

- Een tabel met per gemeten element: kleur, achtergrond, contrastverhouding, in beide modi, vóór en na.
- Elk gerepareerd element haalt in donkere modus ten minste 4,5:1 (of 3:1 voor grote tekst).
- Een poort die voorkomt dat er een nieuwe vaste kleur bijkomt, in de geest van `tests/test_lotc_toolbar_overloop.py`: een test die vaste hexkleuren of vaste toonnamen op de getroffen plekken tegenhoudt. Kan dat niet zonder valse alarmen, zeg dan waarom.
- `uv run pytest tests/ -q` groen, plus `ruff check`, `ruff format`, `pyright`.

## Terugkoppeling tijdens de uitvoering

Drie berichten van de opdrachtgever hebben de richting bijgesteld; de uitkomst per punt:

1. **"Wies doet het wel goed"** (NLDD rechtstreeks, zonder onze componentlaag). Bevestigd:
   het thema deugt, onze laag niet.
2. **"Vooral de FORMULIEREN kloppen niet."** Dit was de grootste vindplaats en zat achter de
   wizardflow, dus niet in een sweep over URL's: de samenvattingstap had 39 stukken tekst
   onder de norm.
3. **"De moduswissel loopt via een inline script."** Gemeten en vrijgesproken: bij de upgrade
   van het eerste component stond `data-scheme` er al, en systeem-donker geeft exact
   hetzelfde als expliciet donker.
