# Introductiepagina

De publieke pagina die vertelt wat ZAD is, voor iemand die binnenkomt en nog geen rechten
heeft. Hij vult het gat dat viel toen de architectuurpagina werd verwijderd (commit
`07b69996`): sindsdien stuurde `/` iedereen naar `/dashboard`, en die vraagt SSO, dus
eindigde elke bezoeker zonder rechten op het inlogscherm zonder ooit gelezen te hebben
waar hij inlogt.

## Waar hij hangt

| pad | wat |
|---|---|
| `/introductie` | de pagina zelf. **Geen `@requires_sso`** - hij is er juist voor wie nog geen rechten heeft |
| `/` | zonder sessie een doorverwijzing hierheen; mét sessie nog steeds naar `/dashboard` |
| voettekst | "Introductie", op elke pagina - voor wie ingelogd is is dat de enige weg ernaartoe |
| `/lotc/bg/introductie` | de proefopstelling, voor de screenshotdekking |

Doorverwijzen en niet renderen op `/` zelf, zodat de pagina een eigen adres houdt dat je
kunt delen en dat ook werkt voor iemand die al is ingelogd.

## De harde eis: alleen wat waar is

Zo'n pagina schrijft zichzelf vol met beloftes, en een belofte die niet klopt is erger dan
geen pagina. Daarom staat de dienstenlijst er **niet als tekst**: `build_lotc_introductie`
(`opi/web/lotc_switch.py`) leest `SERVICE_DEFINITIONS` uit, en naam, technische naam,
omschrijving en icoon zijn letterlijk die van de dienst zelf.

De catalogus wordt in twee lijsten gesplitst, want dat is precies het verschil dat de
pagina vertelt:

| lijst | wat erin zit | nu |
|---|---|---|
| `diensten_zelf` | wat je bij je project kunt aanzetten | 14 |
| `diensten_achtergrond` | `kind=SYSTEM`: draait altijd, niet kiesbaar | 5 |

Diensten met `hidden=True` blijven eraf (`namespace-postgresql-database`,
`namespace-redis`). Dat is anders dan op `/services`, dat ze bewust wél toont omdat je daar
omgevingsvariabelen opzoekt; op een pagina die uitlegt wat je kúnt kiezen is een dienst die
je niet kunt kiezen ruis.

De systeemdiensten zijn ook de onderbouwing van "het platform kijkt met je mee": hun eigen
omschrijving zegt dat `resource-tuning` geheugen ophoogt na een OOM en dat
`deployment-health` beoordeelt wat de waargenomen toestand van een deployment betekent.
Dat is geen marketingzin maar de tekst van de dienst.

Wat er in de tekst staat en niet uit de catalogus komt, is terug te voeren op één document:

| claim | bron |
|---|---|
| nachtelijke ronde over de hele vloot, geheugen en CPU | `features/auto-resource-tuning.md` |
| back-ups van schijven, databases en buckets naar opslag buiten het cluster, versleuteld per project, in te plannen per deployment | `features/backup-system.md`, `features/scheduled-backups.md` |
| zadctl, en dat CLI en Actions langs dezelfde API gaan | `opi/templates_lotc/bg/cli.html.j2`, `bg/actions.html.j2` |

De verwijzingen naar zadctl en de Actions gaan naar de **repositories** en niet naar `/cli`
en `/actions`: die twee dragen `@requires_sso`, dus vanaf hier zou je op het inlogscherm
belanden.

## Hoe het in elkaar zit

| bestand | rol |
|---|---|
| `opi/web/router.py` | `root()` en `introductie()` |
| `opi/web/lotc_switch.py` | `build_lotc_introductie()`: de catalogus in de vorm die de pagina leest |
| `opi/templates_lotc/bg/introductie.html.j2` | de indeling: welke panelen, in welke volgorde |
| `opi/templates_lotc/bg/_introductie-blokken.html.j2` | wat er in die panelen staat |
| `opi/web/lotc_fixtures.py` | de proefopstelling, met de echte catalogus |

De blokken zijn **macro's** en geen deeltemplates die de pagina include't: een `{% include %}`
van een deeltemplate dat zelf componenten gebruikt breekt zodra de schil `<template slot=...>`
gebruikt, en dat doet `base_lotc.html.j2`. Zie de kop van dat bestand, en `bg/about.html.j2`,
dat zijn vier deeltemplates juist daarom heeft samengevoegd.

## Drie dingen die in de HTML klopten en op het scherm niet

Alle drie gevonden door de pagina in een browser op te meten, geen ervan door de HTML te
lezen. Dit is de reden dat `tests/e2e/test_introductiepagina_beeld.py` bestaat naast de
gewone test.

1. **`<c-hero>` rendert niets.** Zijn schaduwboom is letterlijk `<!---->`: het component is
   gedefinieerd maar projecteert geen enkel slot, dus titel, ondertitel, afbeelding én de
   alinea eronder stonden er geen van alle - en de pagina had daarmee geen `<h1>`. Dat
   verklaart ook waarom geen enkele `bg/`-pagina hem gebruikt: hij komt alleen voor op de
   oude architectuurpagina en twee proefpagina's, die door geen route meer gerenderd
   worden. Vervangen door `page_head()`.
2. **Een `c-metric` in een `<c-cluster>` wordt 0 breed.** De twee tellingen waren 155 pixels
   hoog, stonden compleet in de HTML, en waren op het scherm een lege strook. Dezelfde val
   als `<c-stack>` in een tabelcel. Ze horen in een `c-auto-grid`, net als op het dashboard
   en de projectpagina.
3. **De schil bood een anonieme bezoeker "Uitloggen" aan.** `base_lotc.html.j2` tekende het
   accountmenu onvoorwaardelijk, dus rechtsboven stond "Account" met "Profiel" en
   "Uitloggen" - drie bestemmingen die alle drie op het inlogscherm uitkomen - terwijl
   "Inloggen" er niet stond. `get_menu_items()` wist dit al; de schil luisterde er niet
   naar. Nu wel, en dat geldt voor elke publieke pagina.

## Wat er van de oude architectuurpagina is overgenomen

Alleen de **puzzelgedachte** ("Pieces of the Puzzle"), omdat de eigenaar die zelf ook
gebruikt: de architectuurpuzzel is al opgelost, de stukjes liggen op hun plek.

Niet overgenomen: de pagina was 1999 regels Engels, met `rvo-`klassen waar in deze schil
geen regel bij hoort (`base.css` komt niet mee), 85 inline stijlen, een `mermaid`-script van
een CDN, en een hero die niets rendert. Dat is geen lange pagina maar een pagina uit een
ander systeem; er valt niets aan te knippen. Het bestand
(`opi/templates_lotc/architecture-overview.html.j2`) staat er nog en heeft nog steeds geen
route.

Ook niet overgenomen: de promotievideo (`static/promo.mp4`) en de wolkenfoto
(`static/cloud.jpg`). Van de video kan niemand zeggen welke versie van het platform erin te
zien is, en dat is precies dezelfde fout als een verouderde dienstenlijst. De foto is een
stockbeeld dat niets over ZAD zegt, en de rest van dit portaal heeft nergens sierfoto's.

## Testen

| test | bewaakt |
|---|---|
| `tests/test_introductiepagina.py` | dat de route 200 geeft zónder sessie, dat er geen `_requires_sso` op staat, dat `/` een anonieme bezoeker hierheen stuurt, en dat elk label en elke omschrijving gelijk is aan wat de catalogus levert |
| `tests/e2e/test_introductiepagina_beeld.py` | dat het in een browser ook echt te zien is: de kop, elke dienstkaart met afmetingen, geen lege plek waar een icoon hoort, geen plat paneel, de tellingen met echte breedte, en "Inloggen" in plaats van "Uitloggen" voor wie niet is ingelogd |
| `tests/e2e/test_public_pages.py` | dat `/` een bezoeker naar de introductie stuurt en een ingelogde gebruiker naar het dashboard |
| `tests/e2e/test_lotc_visual.py` | de screenshotdekking, via de proefopstelling |

## Wat er open staat

- De **zijkolom** toont een anonieme bezoeker Dashboard, Mijn projecten, Nieuw Project,
  Services overzicht, CLI, Actions en API Docs. Die zeven redirecten allemaal naar het
  inlogscherm. Dat is te verdedigen (het is een uitnodiging om in te loggen) maar het is
  niet besloten; het zit in `get_menu_items()` en raakt elke publieke pagina.
- **`/about` en `/introductie` overlappen.** `/about` ("Waarom dit platform?", "Hoe werkt
  het?", "Veilig ingericht") is publiek en vertelt een deel van hetzelfde verhaal. Ze staan
  nu allebei in de voettekst en de introductie verwijst ernaar. Samenvoegen is een besluit
  van de eigenaar, geen opruimactie: `/about` staat in drie tests en in de voettekst.
- **`<c-hero>` is stuk** en dat is niet bij het LOTC-project gemeld. Als een hero op deze
  pagina alsnog gewenst is, is dat de weg; `request_for_components.md` is de plek.
