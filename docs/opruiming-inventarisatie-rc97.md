# Hoeveel troep is er nog: de inventarisatie

Opgemaakt 13 augustus 2026 (RC-97). Aanleiding: er is de afgelopen weken veel verbouwd en
de vraag was wat er aan restanten is blijven liggen.

Dit is eerst een **inventarisatie**. Per gevonden restant staat hieronder wat het is,
waaruit blijkt dat het dood is, en wat er kapotgaat als het toch niet dood was. Dat derde
is het belangrijkste: "niemand importeert dit" is zwakker bewijs dan het lijkt. Een
sjabloon kan geladen worden via een naam die uit gegevens komt, een klasse kan de haak
zijn waar JavaScript aan hangt, en een test die groen blijft na het weggooien bewijst
niets als hij de verkeerde plek maat.

De vondsten staan in drie bakken:

1. **Aantoonbaar dood** - weg, in een eigen commit met het bewijs in de boodschap.
2. **Waarschijnlijk dood, niet te bewijzen** - blijft staan, staat in `TODO.md` met wat er
   nodig is om het wel vast te stellen.
3. **Leeft nog, maar is dubbel** - dat is geen opruimwerk maar een keuze over welke van de
   twee blijft, en die hoort apart.

Aan het eind staat wat er GEEN restant bleek te zijn, want twee van de vier startpunten
uit het plan hielden geen stand.

---

## Bak 1: aantoonbaar dood, weggehaald

### 1.1 `opi/templates_lotc/project-details.html.j2` en de map `project-details/` (27 bestanden)

**Wat het is.** De vorige vormgeving van de projectpagina: een paginatop met drie
tabbladen, en 26 deeltemplates eronder.

**Waaruit blijkt dat het dood is.** Vier metingen, waarvan de derde de beslissende:

1. Geen python-code noemt `project-details.html.j2` of een bestand uit de map. De route
   die de projectpagina rendert (`opi/web/router.py`) noemt `bg/project-tabs.html.j2`.
2. Geen levend sjabloon includeert er een. Binnen de map includeert alleen de paginatop
   ze; drie bestanden (`_argocd-deployment-card`, `_resource-usage`, `section-tasks`)
   worden zelfs daar niet genoemd en hingen alleen nog aan tests.
3. De paginatop includeerde `project-details/section-pending-rollout.html.j2`, en dat
   bestand bestond in **geen enkel zoekpad** van de Jinja-omgeving. De pagina kon dus niet
   renderen. Was er nog iets dat hem opvroeg, dan was dat een 500 geweest - het feit dat
   niemand dat gemerkt heeft IS het bewijs.
4. Bereikbaar was hij nog wel, en precies op de manier waar het plan voor waarschuwt: de
   allowlist van `/lotc/pagina/<naam>` wordt opgebouwd uit een **mapscan**
   (`_previewable_pages()` in `opi/web/lotc_router.py`), dus de naam komt uit gegevens en
   niet uit de code. Gemeten met een echt verzoek: `422`, want hij rendert daar evenmin.

**Wat er kapotgaat als het toch niet dood was.** De proefopstellingsroute
`/lotc/pagina/project-details` geeft nu een 404 in plaats van een 422. Verder niets: er
is geen weg waarlangs deze sjablonen HTML naar een gebruiker konden sturen.

**Wat er meeverhuisde.** Tien testbestanden rendeerden of lazen een van deze sjablonen en
maten dus niets over de pagina die de gebruiker ziet. Ze wijzen nu naar het `bg/`-sjabloon
dat de route wel rendert. Drie uitspraken konden niet mee - zie bak 3.

### 1.2 De contextsleutel `deployment_state_facts`

**Wat het is.** Een sleutel die de projectpaginaroute per verzoek berekende en meegaf.

**Waaruit blijkt dat het dood is.** Het enige sjabloon dat hem las was
`project-details/section-deployment-state.html.j2`. De herontworpen kaart leest dezelfde
feiten uit `deployment_states`, dat naast de sleutel al werd meegegeven. Na 1.1 geeft een
grep over `opi/templates_lotc/` nul treffers.

**Wat er kapotgaat als het toch niet dood was.** De dienstberichten onder de
deploymentkaart ("dit deployment slaapt") zouden verdwijnen. `tests/test_deployment_state_block.py`
meet die drie uitspraken nu op de kaart die de route rendert, dus dat zou luid falen.

---

## Bak 2: waarschijnlijk dood, niet te bewijzen

Deze staan in `TODO.md`, met wat er nodig is om het wel vast te kunnen stellen.

### 2.1 `lotc-rvo` als dependency

Het pakket staat in `pyproject.toml` en wordt geïnstalleerd, maar staat niet in
`DESIGN_SYSTEMS` (`opi/core/templates_lotc.py` laadt `lotc-layout`, `nldd` en
`lotc-forms`). Het levert dus geen stijl en geen componenten aan de applicatie.

**Waarom het toch niet weg kan.** Er is één echte lezer:
`tests/test_css_dode_variabelen.py` leest het tokenbestand van het pakket
(`lotc_rvo/static/lotc/dist/@nl-rvo/design-tokens/index.css`) als de enige bron die kan
zeggen of een `--rvo-*`-naam ooit heeft bestaan. Zonder het pakket verliest die poort zijn
meetlat. Daarnaast levert het pakket sjablonen die via de kale bouwlijn geladen zouden
kunnen worden; dat het niet in de zoekpaden van de huidige omgeving staat, sluit niet uit
dat een andere omgeving ze wel vindt.

### 2.2 `static/css/project-details.css` (24 kB)

Het bestand wordt nog geladen door `bg/project-tabs.html.j2` en `bg/_modals.html.j2`, dus
het is niet dood. Het is wel geschreven voor de pagina uit 1.1, en hoeveel van de
selectors nog ergens op slaan is niet gemeten. Zie de waarschuwing in het plan: een klasse
zonder CSS is niet vanzelf dood, en een CSS-regel zonder klasse is niet vanzelf
verwijderbaar - er kan JavaScript aan hangen.

---

## Bak 3: leeft nog, maar is dubbel - dat is een keuze

### 3.1 De oude paginaboom naast `bg/`

Zestien pagina's zijn alleen nog bereikbaar via de proefopstellingsroute
`/lotc/pagina/<naam>`; geen enkele echte route rendert ze. Met hun deeltemplates erbij
zijn dat **62 sjablonen, waarvan 57 niet gedeeld** met de rest van de applicatie.

| Oude pagina | Herontworpen tegenhanger |
|---|---|
| `about.html.j2` | `bg/about.html.j2` |
| `admin/approvals.html.j2` | `bg/admin-approvals.html.j2` |
| `admin/usage.html.j2` | `bg/admin-usage.html.j2` |
| `admin/user-form.html.j2` | `bg/admin-user-form.html.j2` |
| `admin/users.html.j2` | `bg/admin-users.html.j2` |
| `dashboard.html.j2` | `bg/dashboard.html.j2` |
| `metrics-explorer.html.j2` | `bg/metrics-explorer.html.j2` |
| `permission-denied.html.j2` | `bg/permission-denied.html.j2` |
| `project-progress.html.j2` | `bg/project-progress.html.j2` |
| `project-progress-done.html.j2` | `bg/project-progress-done.html.j2` |
| `projects-overview.html.j2` | `bg/projects.html.j2` |
| `wizard/wizard_page.html.j2` | `bg/wizard-page.html.j2` |
| `wizard/wizard_start.html.j2` | `bg/wizard-start.html.j2` |
| `architecture-minimal.html.j2` | geen |
| `architecture-overview.html.j2` | geen |
| `project-form-demo.html.j2` | geen |

Dit is **geen opruimwerk**. De vraag is of de proefopstelling "de omzetting naast het
origineel leggen" nog een doel dient nu het herontwerp de echte pagina's rendert. Zolang
dat doel er is, zijn deze pagina's in gebruik en is weghalen een functieverlies. Verschil
met 1.1: die pagina kón niet renderen, deze wel - gemeten, alle zestien geven een 200 of
een 422-met-reden op `/lotc/pagina/<naam>`.

Binnen `bg/` staat een eigen dubbeling: `bg/project-details.html.j2` naast
`bg/project-tabs.html.j2`. Alleen de tweede wordt door de route gerenderd.

### 3.2 Wat het herontwerp anders doet met de dienst op een component

Twee uitspraken uit `tests/test_project_details_service_blocks.py` en
`tests/test_service_help.py` hoorden bij de weggehaalde pagina en gelden niet voor het
herontwerp:

* de oude pagina toonde per dienst een **kaart met icoon en omschrijving**; het tabblad
  Componenten toont een `<c-chip>` met de naam en een vraagteken ernaast;
* de oude pagina liep daarvoor door de `service_block`-macro, het herontwerp niet.

Dat is een vormgevingsbesluit uit de omzetting en geen storing - de naam en de hulptekst
zijn er allebei nog. Het staat hier zodat de keuze zichtbaar is: **wil de projectpagina de
omschrijving van een dienst tonen, dan is dat werk aan het herontwerp**, niet het
terugzetten van een sjabloon.

### 3.3 Team & Toegang staat niet meer op het tabblad Project

`tests/test_project_resource_usage.py` deed over de plaatsing van het resourceblok een
uitspraak in twee helften: **onder de acties, boven het team**. De eerste helft is op
`bg/project-tabs.html.j2` nog gewoon af te lezen (het paneel Acties staat voor het paneel
Resourcegebruik, en dat voor Deployments - precies wat de toelichting in dat sjabloon zelf
zegt: "tussen Acties en Deployments").

De tweede helft is er niet meer af te lezen, en niet omdat de volgorde veranderd is: **Team
& Toegang is een eigen tabblad geworden** en staat helemaal niet meer op dit tabblad. Er is
dus niets meer om "boven" te staan. Die halve uitspraak is daarom vervallen in plaats van
naar een ander anker omgebogen - een test die je zo groen buigt dat hij een andere vraag
gaat stellen, meet vanaf dan niets.

**Wat de keuze is.** Wil je bewaken dat het resourceblok hoog op de pagina blijft ten
opzichte van wat er nu wel onder staat, dan is dat een nieuwe uitspraak over het
herontwerp, niet het herstel van een oude.

### 3.4 De herkomstregels in de `bg/`-sjablonen

Dertien `bg/`-sjablonen verwijzen in hun toelichting naar `project-details/...`. Die bestanden
bestaan sinds 1.1 niet meer. De regels blijven staan: ze vertellen waar de markup vandaan
komt en dat is nog steeds waar; de bestanden zelf staan in de git-historie. Ze aanpassen
zou dertien bestanden raken die verder niets met deze opruiming te maken hebben.

---

## Geen restant: twee startpunten die geen stand hielden

Het plan noemde vier startpunten. Twee ervan bleken bij meting geen restant te zijn. Dat
is de reden om eerst te inventariseren.

### `copyToClipboard` - **leeft**

Het plan vermoedde dat de `{% set kopieer = ... %}`-regels niets meer aanroepen sinds het
kopieerknopje `<c-secret-field show-copy>` werd. Gemeten: drie dienstsjablonen
(`keycloak/section-detail`, `keycloak/otp-code`, `invite/section-detail`) gebruiken die
variabele wel degelijk, in `:attrs="{'onclick': kopieer}"`, en
`bg/project-tabs.html.j2` laadt `static/js/copy_to_clipboard.js`. De klassen
`config-item` / `config-code` / `copy-btn` zijn geen vormgeving maar de haken waarlangs de
functie de waarde terugvindt - precies het geval waar het plan voor waarschuwt. Het enige
dode gebruik stond in de map uit 1.1 en is daarmee weg.

### De `--rvo-*`-variabelen in de CSS - **al opgeruimd, en er staat een poort op**

Het plan noemde "honderden verwijzingen naar variabelen die nergens meer bestaan". Dat was
zo, maar RC-74 heeft het opgelost: `tests/test_css_dode_variabelen.py` telt de
verwijzingen af tegen het tokenbestand plus de shim onderaan `static/css/lotc-app.css`, en
zijn aftellijst `NOOIT_BESTAAN` is **leeg**. De poort faalt op elke nieuwe dode naam.

Alle elf stylesheets onder `static/css/` worden bovendien door minstens één levend
sjabloon geladen; er is geen enkel wees-stijlblad.

---

## Wat deze ronde heeft opgeleverd aan bewaking

`tests/test_sjablonen_verwijzen_naar_bestaande_sjablonen.py`: elke naam in een
`extends` / `include` / `import` / `from` wordt opgezocht in dezelfde Jinja-omgeving als
de applicatie gebruikt. Dat is de poort die de ontbrekende include uit 1.1 zou hebben
gevangen. Jinja lost een include pas op als de regel wordt uitgevoerd, dus zonder zo'n
poort is een verkeerde naam onzichtbaar tot iemand de pagina opvraagt - en op een dode
pagina dus nooit.
