# De schil van fundament als voorbeeld: onderzoek plus proef

**Dit is een onderzoeks- én bouwopdracht, op een eigen tak, met ruimte om te experimenteren.** De uitkomst mag "we doen het niet" zijn; dan is de proef het bewijs waarop dat besluit rust. Er gaat niets naar productie, er wordt niets uitgerold, en de bestaande schil blijft staan tot er een besluit ligt.

## Wat de wens is

De vormgever (Bart van de Biezen) zet in `bartvandebiezen/fundament`, tak `feat/console-nldd-ds`, een console op met het NLDD-designsysteem. De indeling en de menustructuur zijn daar anders dan bij ons, en er zijn iconen bijgekomen. De vraag is drieledig:

1. **Wat is fundament, en wat is daarvan de basis waarop wij verder zouden bouwen?** Niet als code, want de stack verschilt (zie hieronder), maar als indeling en interactiemodel.
2. **Wat is er nodig om ZAD zo om te zetten?** Uitgedrukt in werk, in risico's en in wat er aan de LOTC-kant bij moet.
3. **Blijven we onze eigen componenten uit `lord-of-the-components` (LOTC) gebruiken, of gaan we NLDD rechtstreeks gebruiken?** Dat is de kernvraag en die moet met een meting beantwoord worden, niet met een voorkeur.

Overleggen kan: er draait een sessie `dclaude-lord-of-the-components-1` op het LOTC-project. Gebruik `send-message dclaude-lord-of-the-components-1 "..."` voor vragen en voor het aanvragen van wat er aan die kant moet gebeuren. Kort houden, één onderwerp per bericht.

## Wat er al gemeten is, 23 augustus 2026

Deze getallen staan hier zodat je ze niet opnieuw hoeft te halen. Controleer ze steekproefsgewijs, maar begin er niet mee.

### ZAD vandaag

| wat | stand |
|---|---|
| templates | 198 bestanden, 16.082 regels, in `operations-manager/python/opi/templates_lotc/` |
| componentaanroepen | 2.449 `<c-*>` over 97 verschillende componenten |
| rauwe designsysteem-tags | 115 `<nldd-*>` rechtstreeks in de templates |
| HTML-routes | 82 (`response_class=HTMLResponse` in `opi/web/`) |
| actieve designsystemen | `["lotc-layout", "nldd", "lotc-forms"]` in `opi/core/templates_lotc.py` |
| visuele poort | 56 schermafdrukken in `tests/e2e/screenshots/lotc/`, plus 29 `tests/test_lotc_*.py` |
| LOTC-pin | `762e57090ea32bd6f1c3d717f2fa6c9ac1ef2f4e`, en die pin vendort NLDD **0.8.80** |

De schil staat in `opi/templates_lotc/base_lotc.html.j2` en is de opzet van bg.rijks.app: `nldd-page` met `nldd-top-navigation-bar` als kopbalk (met `nldd-menu-bar` als hulpbalk voor Weergave, Beheer en account), daaronder `c-sidebar-section` met `c-sidenav`, en een `nldd-page-footer`. De indeling van de navigatie staat los van de templates, in `opi/web/navigation_lotc.py` (`GROUPS`, vier groepen) plus de icoonvertaling `ROOS_TO_NLDD_ICONS`.

Belangrijk: **NLDD is al ons thema.** LOTC is niet een ander designsysteem, het is de schrijfweg naar NLDD. Elke `<c-*>` in onze templates komt er als een `<nldd-*>`-webcomponent uit. De vraag "LOTC of NLDD" gaat dus niet over hoe het eruitziet maar over of de compilerlaag ertussen zijn kosten waard is.

### Fundament

`console-frontend` en `dcim-frontend` zijn **Angular 22 met `@nldd/design-system` 0.8.83 en Tailwind 4**, als SPA. Wij zijn FastAPI met Jinja2, htmx en serverrendering. Er is dus geen enkele regel markup letterlijk over te nemen. Wat wél overneembaar is: de indeling, de menustructuur, de plek waar formulieren staan, en de manier waarop lijsten zijn opgebouwd.

Het domein van `console-frontend` lijkt sterk op dat van ons: projects, namespaces, clusters, project-members, invite-member, api-keys, plugins, organization-settings, organization-limits, dashboard, metrics, profile. Dat maakt het een bruikbaar voorbeeld en geen willekeurige app.

**De schil van fundament**, uit `console-frontend/src/app/app.html` (727 regels) en `dcim-frontend/src/app/shell/shell.html`:

- `nldd-app-view` > `nldd-bar-split-view`.
- Een kopbalkpaneel (`slot="header"`) met één `nldd-toolbar`: logo plus productnaam links, en rechts twee dingen. Een `+`-knop (`nldd-icon-button` met `popup-type="menu"`) voor alles wat je kunt maken, want "iets nieuws maken" is geen eigenschap van de pagina waar je toevallig staat. En één avatarknop met alles over je account erin: wie je bent, Appearance als radiogroep (systeem, licht, donker) en uitloggen.
- Een hoofdpaneel met `nldd-navigation-split-view` in drie kolommen: het sectiesmenu, een tweede kolom die de **pagina** vult (rack-lijst, categorieën, filters), en de inhoud. Terug-navigatie en het inklappen naar één kolom lopen via `(back)` en `(nldd-single-column-change)`.
- Het sectiesmenu is geen `sidenav` maar een `nldd-list type="navigation"` met per item een `nldd-icon-cell`, een `nldd-text-cell` en optioneel een `nldd-badge` met een telling of een statusstip. Dat is de reden dat het een menu is en geen rij tabbladen: een tabblad heeft geen plaats voor een telling en houdt bij zes items op.
- **Formulieren staan niet in de pagina.** Ze hangen als `nldd-sheet` bóven de app, buiten de router, zodat ze de pagina overleven waarvandaan je ze opende. 42 sheets, 39 `nldd-modal-dialog`, 57 `nldd-inline-dialog`.
- Elke pagina draagt een `nldd-top-title-bar` (77 keer), die ook de weg terug draagt zodra de kolommen inklappen.
- Lijsten in plaats van tabellen: 361 `nldd-text-cell`, 315 `nldd-spacer-cell`, 221 `nldd-list-item`, en welgeteld één `nldd-table` in de hele codebase.

Twee dingen zijn nu al belangrijk voor ons. **Fundament zet de toestand van een formulier in de URL** ("open een formulier met `?new=1`", commit 1793a3c). Dat is precies wat een servergerenderde applicatie van nature kan, en het maakt het sheet-patroon minder SPA-gebonden dan het lijkt. En **de commitgeschiedenis is de motivatie**: de commits dragen uitgeschreven redenen ("een rij van zes plussen zegt zes keer hetzelfde", "de zijbalk scrollt, en het huidige item krijgt `current`"). Lees ze; ze zijn een groot deel van de opbrengst van deze opdracht.

### Wat LOTC al dekt

Fundament gebruikt 83 verschillende `nldd-*`-elementen. `lotc-nldd` kent er 76 al, want die renderers zijn gegenereerd uit het custom-elements-manifest van NLDD. **De hele schilwoordenschat van fundament zit er dus al in**: `app-view`, `bar-split-view`, `navigation-split-view`, `split-view-pane`, `toolbar`, `toolbar-title`, `toolbar-item`, `top-title-bar`, `sheet`, `list`, `list-item`, `collection`.

Wat ontbreekt: `box`, `checkbox`, `identity`, `notification`, `page`, `page-footer`, `top-navigation-bar`. De laatste drie gebruiken wij vandaag al als rauwe tag in de schil, dus dat is een bekende weg en geen blokkade.

### De iconen

76 iconnamen in fundament. Veertien zitten niet in onze bundel: `add-plugin`, `all-tasks`, `appearance`, `display`, `folder-on-folder`, `kanban`, `new-folder`, `new-kubernetes`, `new-namespace`, `rack-server`, `rack-servers`, `server`, `square-grid-2x2`, `tools`. Een deel is DCIM-specifiek (de rackiconen), maar `appearance`, `display`, `new-folder`, `new-namespace`, `square-grid-2x2`, `tools` en `server` zijn gewone console-iconen.

`folder-on-folder` staat met naam en toenaam in `navigation_lotc.py` als "bestaat in de lijst van LOTC maar niet in de bundel die de browser laadt". Fundament gebruikt hem wel. Dat is hetzelfde gat, aan onze kant gemeten.

**De nieuwe iconen komen niet uit fundament, ze komen uit de versiesprong van het designsysteem.** Fundament draait NLDD 0.8.83; onze pin vendort 0.8.80; LOTC HEAD (`d19c7a5`) heet "Adopt NLDD 0.8.83". De eerste stap is dus de pin verzetten en meten wat er verandert, niet iconen naschrijven. Let op: er zitten 279 commits tussen onze pin en LOTC HEAD, waaronder codegen-, escaping- en attribuutwijzigingen. Dat is een op zichzelf staande stap met een eigen risico.

## De kernvraag: LOTC houden of NLDD rechtstreeks

Er zijn drie richtingen, en de derde is wat we vandaag feitelijk al doen.

**A. LOTC houden zoals nu.** Alles via `<c-*>`, en waar LOTC iets niet kent vragen we het aan bij het LOTC-project.

**B. NLDD rechtstreeks schrijven.** `<nldd-*>` in de Jinja-templates, geen compilerlaag. We verliezen de attribuutvalidatie, de escaping-laag, de designsysteem-onafhankelijkheid en `<c-page>` die de `<head>` bedraadt. We winnen dat elk voorbeeld uit fundament en uit de NLDD-documentatie letterlijk overneembaar is, en dat er geen tweede project meebeweegt bij een versiebump.

**C. Gemengd, met een expliciete grens.** LOTC voor alles wat een contract heeft dat wij zelf bewaken (formuliervelden, tabellen, onze eigen `secret-field` en `data-list`), rauwe NLDD-tags voor de schil en voor de layoutregio's. Dat is de facto de huidige toestand: 2.449 `<c-*>` naast 115 rauwe tags, en de schil is al grotendeels rauw.

Beoordeel elke richting op dezelfde assen, met een meting per as en niet met een indruk:

| as | waar je het aan afmeet |
|---|---|
| dekking | hoeveel van de fundament-schil je kunt bouwen zonder een nieuwe component aan te vragen |
| snelheid van overnemen | kun je een voorbeeld uit fundament of uit de NLDD-docs letterlijk plakken, of moet het vertaald |
| veiligheid | wat verlies je aan escaping en attribuutvalidatie (zie `opi/forms/lotc_attrs.py` en de escaping-commits in LOTC) |
| versiebeweging | wat kost een sprong van NLDD 0.8.83 naar 0.9 in elk van de drie |
| formulierlaag | `lotc-forms` levert vandaag negen veldtypen plus foutbedrading; wat kost het die zelf te dragen |
| terugvalpad | kun je halverwege van mening veranderen, en wat kost dat |

De uitkomst is een aanbeveling met een getal eronder. Als het antwoord "C, met de grens op deze plek" is, schrijf de grens dan op als een regel die een toets kan bewaken, niet als een richtlijn.

## Deel 1: de analyse

Opleveren in `plans/`, Nederlands, in de vorm van de bestaande documenten daar. Lees er twee voordat je begint, bijvoorbeeld `plans/naar-het-nieuwe-componentensysteem.md` (dat is de vorige omzetting, met dezelfde soort meting) en `plans/meldingen-onderzoeksopdracht.md`. Geen em-dashes. Alinea's op één regel, niet handmatig afgebroken. Verzin geen namen die als vaststaand overkomen: markeer een zelfbedachte naam expliciet als voorstel.

1. **`plans/fundament-de-schil-ontleed.md`.** Wat fundament doet en waarom, per onderdeel van de schil: kopbalk, sectiesmenu, tweede kolom, inhoud, sheets, titelbalk, lijsten, meldingen. Per onderdeel: wat het is, welke NLDD-elementen het gebruikt, wat de motivatie in de commit is, en wat het equivalent bij ons vandaag is. Neem schermafdrukken op als je de console aan de praat krijgt; lukt dat niet binnen redelijke tijd, werk dan uit de markup en zeg dat erbij.

2. **`plans/lotc-of-nldd.md`.** De kernvraag hierboven, met de zes assen, per richting ingevuld en met een aanbeveling. Dit is het document waar de rest op rust, dus schrijf het voordat je gaat bouwen en pas het aan als de proef je van gedachten doet veranderen. Schrijf op wat de proef aan het oordeel veranderd heeft.

3. **`plans/zad-op-de-fundament-schil.md`.** Wat er nodig is om ZAD zo om te zetten, uitgedrukt in fasen met per fase een verifieerbare uitkomst. Hierin horen in elk geval: de LOTC-pinsprong naar NLDD 0.8.83, de nieuwe schil, de herindeling van de navigatie (welke van onze 82 routes hangt waar), de vraag wat er in de tweede kolom hoort per sectie, en wat er met de wizard gebeurt. Geef per fase een grove maat en zeg erbij waar de maat op rust.

4. **De open punten die niet vanzelf oplossen**, met per punt: wat het is, waar het zit, wat het voorstel is, en welke beslissing openstaat. Dat mag een eigen document zijn of een hoofdstuk in het vorige.

## Deel 2: de proef bouwen

Bouw op een eigen tak. Dit is nadrukkelijk een proef: het mag lelijk aflopen, en dat is dan de uitkomst.

**Waar de proef staat.** Niet als vervanging van de bestaande schil, en niet achter een schakelaar in de schil zelf: die schakelaar is er bij de vorige omzetting bewust uit gehaald en die komt niet terug. Zet de proef onder een eigen routeprefix, zoals `/lotc/` dat vroeger was, met de bestaande voorbeeldgegevens uit `opi/web/lotc_fixtures/`. Dan is hij te bekijken zonder cluster en zonder de lopende applicatie aan te raken. **Let op:** de `/lotc/`-routes zijn publiek en zitten in de release-image; wat de proef aanlevert ligt dus op straat, dus alleen zichtbaar verzonnen waarden, ook voor de beheerderspagina's.

Voorstel voor de fasering. Wijk ervan af als de meting je iets anders vertelt, maar zeg dan waarom.

1. **De pin verzetten naar LOTC HEAD (NLDD 0.8.83).** Verifieer: `uv run pytest tests/test_lotc_*.py -q` groen, de 56 schermafdrukken opnieuw gedraaid en de verschillen per stuk verklaard (niet massaal bijgewerkt), en de veertien ontbrekende iconen opnieuw gemeten. Dit is de eerste stap omdat alle andere erop leunen, en het is de stap die als enige ook zonder de rest waarde heeft. Loopt hij vast, meld dat dan bij `dclaude-lord-of-the-components-1` en ga door met de rest op de oude pin.
2. **De schil, kaal.** `nldd-app-view` > `nldd-bar-split-view` met de toolbar (logo, productnaam, `+`-menu, avatarmenu met weergave en uitloggen) en `nldd-navigation-split-view` met het sectiesmenu als `nldd-list type="navigation"`. Verifieer: de schil staat, hij scrollt, hij klapt netjes in op één kolom, en licht/donker werkt nog via `data-scheme` (zie de `scheme_script` in `base_lotc.html.j2`, die staat er om een lichtflits te voorkomen).
3. **De navigatie herindelen.** `GROUPS` in `navigation_lotc.py` is één plek en dat moet zo blijven. Beslis waar de vier groepen heen gaan, wat er in het `+`-menu hoort (wat kan een gebruiker maken: project, deployment, component, dienst, gebruiker) en wat er in de tweede kolom hoort per sectie. Verifieer met een toets die de indeling leest, zoals `test_lotc_icon_mapping.py` dat voor de iconen doet.
4. **Drie pagina's omzetten**, en kies ze op verschil, niet op gemak: het projectoverzicht (een lijst), de projectdetailpagina (tabbladen, htmx-fragmenten, de tweede kolom heeft hier echt iets te doen) en één beheerpagina met een formulier. Verifieer: de pagina's staan er, ze werken met htmx, en er staan schermafdrukken naast de oude.
5. **Een formulier in een sheet.** Dit is het echte risico, dus doe het als aparte fase en niet als bijvangst van fase 4. Neem het patroon van fundament over: de toestand in de URL (`?nieuw=1` of iets dergelijks, naam als voorstel), zodat de server weet of de sheet open moet en een verversing hem niet dichtslaat. Verifieer met een Playwright-toets: openen, invullen, fout laten geven, sluiten, en terug met de browserknop.
6. **De wizard aankijken en er iets van vinden.** Niet omzetten. De wizard is meerstaps, htmx-gedreven en heeft eigen fragmenten (`bg/wizard-page.html.j2` en drie fragmenten); die past niet vanzelfsprekend in een sheet. Schrijf op wat de opties zijn en wat elk kost. Dit is de fase waarvan het antwoord "dit past niet, en daarom moet de schil op dit punt van fundament afwijken" mag zijn.

**Wat je onderweg moet blijven doen.** Werk in kleine commits met uitgeschreven waarom, zoals fundament zelf doet. Meet, ga niet af op wat er zou moeten werken: de commentaren in `base_lotc.html.j2` staan vol met dingen die logisch klonken en niet werkten (de bovengrens van de breedte komt uit `width=` op de regio's en niet uit een wikkel; `nldd-page` als kale wikkel breekt het vensterscrollen). Bewaar dat soort bevindingen in het document, niet alleen in een commit.

**Twee valkuilen die we al kennen.** Webcomponenten met `ElementInternals` leveren hun waarde niet mee aan htmx; daar staat `static/js/form-associated.js` voor, en die moet in de nieuwe schil ook geladen worden (zie `features/aanvinkvakje.md`). En `<c-page>` bedraadt zelf de `<head>` inclusief de CSS en JS van de actieve designsystemen; ga die niet met de hand nabouwen.

## Deel 3: wat het LOTC-project moet leveren

Verzamel dit tijdens het bouwen en stuur het per onderwerp naar `dclaude-lord-of-the-components-1`, niet als één lijst aan het eind. Wat er nu al op staat:

- De zeven componenten die fundament gebruikt en `lotc-nldd` niet kent: `box`, `checkbox`, `identity`, `notification`, `page`, `page-footer`, `top-navigation-bar`. Vraag of die uit de codegen kunnen komen; de rest komt daar ook vandaan.
- De veertien iconen. Vraag of ze met 0.8.83 in de bundel zitten, en specifiek wat er met `folder-on-folder` gebeurt, want die staat aan onze kant al als alias-naar-niets gemeld.
- Wat je in fase 1 tegenkomt bij de pinsprong.
- Alles waarvan je merkt dat je het met een rauwe tag moet oplossen. Dat is de lijst die richting C tot een bewuste keuze maakt in plaats van tot een verzameling uitzonderingen.

Sluit elk bericht af met wat je zelf al geprobeerd hebt, zodat er aan de andere kant niets dubbel gebeurt.

## Poorten

- `cd operations-manager/python && uv run ruff check . --fix && uv run ruff format . && uv run pyright`
- `uv run pytest tests/test_lotc_*.py -q` groen.
- `uv run pytest tests/e2e -m "e2e and not sandbox" -q` groen, en elke gewijzigde schermafdruk verklaard.
- Geen em-dashes in de opgeleverde documenten; grep erop voor je afrondt.
- De bestaande schil doet het nog. Als de proef de huidige applicatie stukmaakt is de proef fout, niet de applicatie.

## Wat expliciet niet in scope is

De Angular-kant van fundament overnemen, of ook maar overwegen. Wij zijn servergerenderd met htmx en dat blijft zo binnen deze opdracht. Verder: niets uitrollen, niets naar GitHub pushen, de bestaande schil niet vervangen, en de wizard niet omzetten.

## Open beslissingen

Deze horen aan het eind beantwoord in de documenten, niet halverwege stilletjes ingevuld:

1. LOTC, NLDD rechtstreeks, of gemengd met een bewaakte grens. En als het gemengd wordt: waar ligt de grens precies.
2. Blijft de voettekst bestaan? Fundament heeft er in de console geen, en bij ons hangen daar vier bestemmingen aan die anders alleen nog via het menu bereikbaar zijn.
3. Gaat "Weergave" van een eigen menu-item in de hulpbalk naar het accountmenu, zoals fundament het doet? Dat is een instelling die je twee keer per jaar aanraakt naast dingen die je de hele dag gebruikt.
4. Krijgt het sectiesmenu tellingen en statusstippen, en zo ja waarvan? Wachtende aanvragen en ongezonde deployments zijn de twee kandidaten die zich opdringen.
5. Wat hoort er in de tweede kolom per sectie, en is er een sectie waar hij leeg blijft? Een kolom die op de helft van de pagina's leeg is, is een kolom te veel.
6. Wat gebeurt er met de wizard.
