# Naar het nieuwe componentensysteem

Status: inventarisatie plus plan, 7 augustus 2026. Niet gebouwd. Aanleiding: er is een nieuw componentensysteem (Lord of the Components, LOTC) en het voornemen om OPI in de vormgeving van [bg.rijks.app](https://bg.rijks.app/) te gieten, via de NLDD-implementatie.

Alle getallen hieronder zijn gemeten op 7 augustus, op `operations-manager/python` en op de branch `plan-v7-lotc-thema-agnostische-compiler-performanc` van `lord-of-the-components` (taak PR-1, status approved).

## Deel 1: wat wij nu hebben

```
110 templates, 12.179 regels
1.280 <c-*> componentaanroepen
  646 kale <div>
  627 regels eigen CSS (wizard.css 441, modal.css 186)
  260 inline style= attributen
   17 <style>-blokken in templates
   15 macro-definities, in 28 bestanden geimporteerd
   71 herhaalde blokken van 6+ regels over meerdere bestanden
```

**Het goede nieuws staat bovenaan.** We zijn al grotendeels een componenten-applicatie: 1.280 componentaanroepen tegen 646 kale `div`s, en bijna geen kale `button` (10) of `table` (5). De zwaarste gebruikers zijn `c-layout-flow` (294), `c-icon` (226), `c-heading` (176), `c-card` (165) en `c-button` (82).

**Het opruimwerk zit in drie dingen.**

*Inline styling.* 260 `style=`-attributen en 17 `<style>`-blokken in templates. De zwaarste zijn `architecture-overview.html.j2` (85 inline styles plus een eigen `<style>`), `_argocd-deployment-card.html.j2` (28) en de restore-partials (12 en 11). Dat is vormgeving die in geen enkel design system meeverhuist en die bij een omzetting stilletjes blijft staan.

*Duplicatie.* 71 blokken van zes regels of meer komen in meer dan een bestand voor. De zwaarste paren zijn `task_progress_fragment` en `modal_wizard_progress_fragment` (17 gedeelde blokken), en `project-creation-partial` en `project-creation-success` (10). Dat zijn kandidaten voor een macro, en het is precies het soort duplicatie dat een omzetting twee keer laat doen.

*Eigen CSS.* 627 regels. Een deel is echt nodig (de dienstkaarten), een deel beschrijft dingen die een design system zelf hoort te leveren.

## Deel 2: wat LOTC biedt

LOTC is een **design-system-agnostische compiler** voor Jinja2: je schrijft `<c-*>`-tags en wisselt van design system met een argument (`design_systems=["rvo"]` of `["nldd"]`), zonder de templates aan te raken. Componentdefinities staan centraal in core; elk design system implementeert een deel.

Dat is precies het model dat wij nodig hebben, en het sluit aan op wat we al doen: wij schrijven al `<c-*>`.

**De bg.rijks.app-nabouw is klaar.** `tests/visual/BG_OVERZICHT_GAPS.md` meldt de status als opgelost: de Overzicht-, Softwarecatalogus- en Mijn-overzicht-pagina's zijn volledig uit thema-agnostische `<c-*>`-componenten opgebouwd en renderen door **zowel NLDD als RVO**. De eerdere gatenlijst is weggewerkt, en het aparte `lotc-bgnldd`-pakket is verwijderd (de README noemt het nog wel; die is verouderd).

Er zijn daarbij samengestelde componenten ontstaan die wij zouden willen hebben: `c-app-shell`, `c-metric`, `c-sidenav`, `c-catalog-card`, `c-filter-bar`, `c-detail-list`, `c-notification`, `c-status-bar`.

## Deel 3: het gat, gewogen

Niet hoeveel namen ontbreken telt, maar hoeveel gebruik eronder zit:

```
1.280 <c-*> aanroepen in onze templates
1.082 gedekt door LOTC   (84%)
  198 niet gedekt        (15%)
```

Van die 198 is ongeveer de helft een **hernoeming** en geen gat:

| onze naam | aantal | in LOTC |
|---|---|---|
| `c-p` | 42 | `paragraph` |
| `c-menubar` | 10 | `menu` |

Wat overblijft zijn twee echte gaten:

**De lijstprimitieven** (`ul` 40, `li` 15, `tr`/`thead`/`tbody` 8). LOTC heeft `data-list` en `table`, maar geen kale lijst. Klein om toe te voegen.

**De formulierlaag** (`text-input-field` 28, `secret-field` 9, `fieldset` 9, `action-group` 8, `select-field` 5, plus `date-input-field`, `file-input-field`, `textarea-field`, `radio-button-field`, `checkbox-field`). De core-woordenschat heeft de primitieven (`checkbox`, `radio`, `select`, `text-input`, `textarea`) maar niet de veld-omhulsels: label, hulptekst, foutmelding en invoer als een geheel.

### En dat gat zit niet in NLDD

Dit is de belangrijkste correctie op een eerdere lezing van deze inventarisatie. NLDD heeft de formulierlaag wel degelijk, en rijk: **21** componenten in zijn registry.

```
text-field  password-field  date-field  number-field  multi-line-text-field
search-field  checkbox-field  radio-button-field  radio-button-group
form  form-field  form-field-error-text  form-field-help-text
form-section  form-actions  combo-box  dropdown  switch-field  token-field
```

Onze namen vallen daar bijna een-op-een op:

| onze naam | aantal | NLDD |
|---|---|---|
| `text-input-field` | 28 | `text-field` |
| `secret-field` | 9 | `password-field` |
| `fieldset` | 9 | `form-section` |
| `action-group` | 8 | `form-actions` |
| `select-field` | 5 | `dropdown` of `combo-box` |
| `checkbox-field` | | `checkbox-field` (zelfde naam) |
| `radio-button-field` | | `radio-button-field` (zelfde naam) |
| `date-input-field` | | `date-field` |
| `textarea-field` | | `multi-line-text-field` |
| `file-input-field` | | geen zichtbare tegenhanger |

**Het gat zit dus in de core-woordenschat, niet in de implementatie.** NLDD kan die velden renderen; er is alleen nog geen thema-agnostisch `<c-*>`-component dat erop afbeeldt. Dat is een wezenlijk andere klus dan een formulierlaag bouwen: het is definities toevoegen die op bestaande implementaties wijzen.

Wat dat voor RVO betekent is een open vraag: als een core-definitie er komt, moet RVO hem ook implementeren, en daar hebben we vandaag onze `c-text-input-field` van jinja-roos-components.

## Past het in een keer?

Waarschijnlijk niet, maar de reden is minder zwaar dan het leek. 84 procent gaat mee zoals het is, een deel van de rest is een hernoeming, en de formulierlaag bestaat aan de NLDD-kant al. Wat resteert is het definieren van core-componenten voor die velden, en dat is werk in het LOTC-project.

De kans dat het in een keer past hangt op precies een vraag: worden die core-definities er, en implementeert RVO ze ook. Zolang dat open staat, is een fasering veiliger dan een grote sprong.

## De indeling verandert ook, en dat bepaalt wat fase 1 moet opleveren

Uitgangspunt is de bg.rijks.app-app, en niet alleen haar vormgeving: ook de structuur en indeling. Het eindbeeld is die schil, opgebouwd uit LOTC-componenten (`c-app-shell` met header-, sidebar-, main- en footer-regio's, `c-sidenav`, `c-section-head`), niet onze huidige indeling in een nieuw jasje.

Zo staan onze pagina's er nu voor:

```
 20 templates breiden base.html.j2 uit
  5 soorten blokken in totaal (content, page_title, additional_styles,
    additional_scripts, title)
 21 content-blokken, het grootste 1.509 regels
 35 includes over 26 deeltemplates
```

Vijf bloksoorten voor twintig pagina's betekent dat vrijwel alles in een enkel `content`-blok zit. Een pagina is daarmee een blok tekst en geen samenstelling, en dat is precies wat een herindeling duur maakt: je kunt niets verplaatsen zonder markup te verhuizen. `architecture-overview.html.j2` is het uiterste geval met 1.509 regels in een blok en 85 inline styles.

**Dat scherpt fase 1 aan.** Het doel is niet mooiere markup maar verplaatsbare brokken. Investeer niet in het poetsen van markup die straks toch door LOTC-componenten vervangen wordt; trek de *grenzen* eruit (een pagina wordt een samenstelling van benoemde blokken) en laat de inhoud van die blokken voorlopig zoals hij is. Inline styling en duplicatie horen daar wel meteen uit, want die verhuizen anders mee.

## Navigatie is het startpunt

Het idee blijft hetzelfde als vandaag: een menu, eventueel een submenu, en tabjes. Dat is de navigatie waarmee als eerste gespeeld kan worden, en dat is om drie redenen de juiste keuze.

Ze is **klein**: 32 aanroepen in totaal (`c-menubar` 13, `c-header` 12, `c-tab-item` 3, `c-footer` 3, `c-tabs` 1). Ze is **zichtbaar**, dus of het klopt zie je meteen. En ze hangt aan **geen enkele formulierlogica**, dus de laag die het dunst gedekt is blijft erbuiten.

De afbeelding is bovendien bijna een-op-een:

| onze navigatie | in LOTC |
|---|---|
| `c-menubar` | `c-menu type="bar"`, in de utility-slot van `c-header` |
| submenu | `c-sidenav` + `c-sidenav-group` + `c-sidenav-item`, met actieve staat via `aria-current` |
| `c-tabs` / `c-tab-item` | `c-tabs` / `c-tab` |
| schil eromheen | `c-app-shell`, met slots voor header, sidebar en footer |

Daarmee is de navigatie ook de **proef op de som voor de hele gereedschapsketen**: installeren, `setup_components` met het gekozen design system, de statische bestanden erbij, en een visuele test die de uitkomst vastlegt. Dat allemaal een keer doorlopen op iets kleins is meer waard dan een grote omzetting die op stap een strandt.

## Voorstel, in die volgorde

**Fase 1: opdelen en opruimen.** Elke pagina wordt een samenstelling van benoemde blokken in plaats van een blok van honderden regels, en inline styling en duplicatie gaan eruit. Onafhankelijk van LOTC bruikbaar. Verifieerbaar: het aantal `style=`-attributen, `<style>`-blokken en herhaalde blokken daalt aantoonbaar, het aantal deeltemplates stijgt, en er staat een test die de terugval tegenhoudt.

**Fase 2: de hernoemingen.** `c-p` naar `paragraph`, `c-menubar` naar `menu`. Mechanisch, en het verkleint het gat met de helft.

**Fase 3: de twee gaten dichten, in LOTC en niet bij ons.** De lijstprimitieven en de formulierlaag horen in core thuis, niet als uitzondering in onze templates. Dat is een gesprek met het LOTC-project, geen werk in deze repo.

**Fase 3b: de navigatie omzetten, als proef.** Menu, submenu en tabjes, plus de schil eromheen. Hier wordt de keten voor het eerst end-to-end gelopen. Verifieerbaar met een screenshot-vergelijking, en met de bestaande e2e-navigatietest die gewoon groen moet blijven: de links moeten nog steeds werken, hoe ze er ook uitzien.

**Fase 4: omzetten per pagina, met een visuele test per stap.** Niet alles tegelijk. Begin met een pagina die veel toont en weinig doet (het projectenoverzicht), en eindig met de wizard. Elke pagina komt eruit als een samenstelling van LOTC-componenten in de bg-indeling; de blokken uit fase 1 zijn de eenheden die je daarbij verplaatst.

**Fase 5: de schil.** `base.html.j2` is nu `c-page` met een handgeschreven `rvo-demo-page`-div, header en footer. Die wordt `c-app-shell` met zijn regio's. Dit gaat als laatste, want elke pagina hangt eraan, en pas als de pagina's zelf samenstellingen zijn is de schil verwisselbaar zonder alles tegelijk aan te raken.

## De antwoorden van het LOTC-project

Binnengekomen op 7 augustus, gemeten tegen hun code van vandaag, met NLDD gepind op 0.8.72. Volledig na te lezen in hun repo als `docs/ANTWOORDEN-RIG-CLUSTER.md`.

**1. Core-definities voor de veld-omhulsels: nog niet besloten, richting is ja.** Vandaag bestaan ze niet. De rijke NLDD-veldlaag is een *thema-gebonden* fragment, gegenereerd uit de NLDD custom-elements-manifest, en resolvet dus niet onder RVO. De richting is wel ja, want het past in de missie en het is dezelfde lift die ze al deden voor de app-componenten. Maar het is niet gebouwd en niet ingepland. **Hun advies: plan er niet hard op.** En hun aanbod: zeg het, dan zetten ze het vooraan.

**2. RVO zou die wrapper ook krijgen: ja, als intentie.** Twee redenen dat dat haalbaar is: jinja-roos heeft de onderliggende wrapper al (onze `c-text-input-field`), dus de RVO-implementatie kan die inpakken; en hun vaste patroon is een nieuw core-component meteen in beide thema's te leveren. Uitdrukkelijk: een omzetting naar een core-wrapper is bedoeld om ons roos-gebruik te behouden, niet te vervangen. Wel met een slag om de arm, want gedeeltelijke dekking is toegestaan.

**3. `file-input-field` is een bevestigd gat.** In de NLDD-manifest zit geen file- of upload-element. RVO heeft het wel. Komt er ooit een core-wrapper, dan heeft NLDD een eigen implementatie nodig, of het blijft daar een gat en valt terug op een zichtbare placeholder.

**4. De namen zijn stabiel.** De core-componenten (onze 84 procent) zijn handgeschreven en stabiel; de grote hernoeming (`name` naar `label`) is al geland en er staat niets meer gepland. De NLDD-afgeleide laag is gepind op een exacte versie, met `npm run nldd:diff` om vóór een bump te zien wat er schuift. **1280 aanroepen omzetten is dus redelijk veilig**, mits tegen een gepinde versie.

**5. `lotc-layout` is in de praktijk vereist.** Formeel opt-in, maar de structuurprimitieven (`app-shell`, `grid`, `stack`, `columns`, `bar`) renderen uitsluitend via dat pakket; zonder valt `<c-stack>` terug op `<div class="lotc-unimplemented">`. NLDD levert de vormgeving, `lotc-layout` de structuur. Dus `design-systems="lotc-layout nldd"` is de juiste combinatie en geen toevalligheid.

En de README is inmiddels rechtgezet.

### Wat dat betekent voor de fasering

De enige echte blokkade is punt 1, en die is geen technisch probleem maar een prioriteitsvraag bij het LOTC-project. Fase 1 en 2 kunnen zonder. Fase 3b (de navigatieproef) kan ook zonder, want navigatie raakt de formulierlaag niet. Fase 4 loopt vast op de wizard zolang de veld-omhulsels er niet zijn.

**Er ligt dus een beslissing:** vragen we het LOTC-project om de veld-omhulsels vooraan te zetten? Zo ja, dan is de weg vrij. Zo nee, dan houdt de omzetting op bij de niet-formulierpagina's, en dat is nog steeds een groot deel.

## Wat er aan de LOTC-kant uitgezocht moet worden (beantwoord, hierboven)

Dit hoort gevraagd te worden aan de nog draaiende sessie, niet geraden:

1. **Is de formulierlaag voorzien in core, of bewust buiten scope?** Dat bepaalt of fase 3 een verzoek is of een eigen bouwopdracht.
2. **Hoe verhoudt de NLDD-registry zich tot de core-namen?** De registry telt 97 componenten met een heel eigen woordenschat (`text-field`, `form-field`, `app-view`, `toolbar`), en hoe die op de core-namen afgebeeld worden is uit de branch niet af te lezen.
3. **Wat is de status van `lotc-layout`?** De bg-nabouw gebruikt `design-systems="lotc-layout nldd"`, dus die lijkt verplicht naast een design system.
4. **Hoe stabiel is de component-API?** Wij zouden 1.280 aanroepen omzetten; als de namen nog schuiven, is fase 2 verspilde moeite.

## Waar op te letten

**Vormgeving controleren vraagt visuele tests, en die hebben we niet.** We hebben Playwright-tests die gedrag controleren, geen die vormgeving vastleggen. Voor een omzetting als deze is een screenshot-vergelijking per pagina het enige dat "het ziet er nog goed uit" hard maakt. LOTC heeft die opzet al (`screenshots/recreate/`, `tests/visual/`), dus die kant is te lenen.

**De formulierlaag is geen markup maar gedrag.** Onze velden hangen aan editables, validators, converters en foutweergave. Een `c-text-input-field` vervangen door een primitief plus handmatig label is een stap achteruit die je pas merkt als de foutmeldingen wegvallen.

**Fase 1 is waardevol zonder de rest.** Als de omzetting niet doorgaat, is het opdelen nog steeds winst. Dat is de reden om ermee te beginnen en niet om erop te wachten.

**Poets geen markup die vervangen wordt.** De verleiding bij fase 1 is om alles netjes te maken. Alles wat straks een LOTC-component wordt, hoeft alleen op de goede plek te staan, niet mooi te zijn. Grenzen trekken loont, inhoud herschrijven niet.

**`architecture-overview.html.j2` verdient een eigen besluit.** 1.509 regels in een blok, 85 inline styles, een eigen `<style>`. Dat is geen pagina om mee te nemen in een omzetting; het is er een om apart te beoordelen, en misschien om te vervangen in plaats van om te zetten.

**De README van LOTC is op een punt verouderd** (hij noemt `lotc-bgnldd`, dat verwijderd is). Verifieer de andere aannames in dit plan tegen de branch en niet tegen de documentatie.
