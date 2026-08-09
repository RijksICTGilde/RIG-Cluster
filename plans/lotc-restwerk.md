# Restwerk omzetting naar het nieuwe componentensysteem (LOTC/NLDD)

Stand: 2026-08-09. Branch `naar-het-nieuwe-componentensysteem`, laatste uitrol `a4713533`.

## De opdracht, in één zin

Neem de bestaande pagina's over met de nieuwe vormgeving en behoud alle functionaliteit.
**Niet herontwerpen, niet weglaten, niet verzinnen.** Elke keer dat dat toch gebeurde
kostte het een ronde: de infoknop werd een inline link, de deploymentkiezer verdween, het
wizardformulier kreeg een kolom van 46rem, de servicekaart verloor zijn variabelen.

## Werkwijze per onderdeel (aangedragen door de gebruiker, en die werkt)

1. **Wat is er nu** — open het origineel, lees het sjabloon, maak een screenshot.
2. **Hoe zet ik dat om** — welk component, welk gedrag hangt aan welke markup.
3. **Omzetten.**
4. **Visueel checken** — screenshot maken en ZELF bekijken.
5. **Functioneel checken** — in de browser klikken en meten wat er verstuurd wordt.
6. **Bij fout: herhalen**, niet doorgaan.

Stap 4 en 5 zijn degene die overgeslagen werden. Alle fouten die de gebruiker vond waren
onzichtbaar in de HTML en in de gedragsmeting: een vakje dat niet aanvinkt, een knop
zonder tekst, een formulier op halve breedte.

## Gereedschap

- `scripts/lotc_compare_behaviour.py` — vergelijkt het GEDRAG van een pagina in beide
  weergaven (links, htmx-adressen, JS-aanroepen, velden, id's) én meldt ACHTERSTAND
  (hoeveel oude markup er nog in de nieuwe weergave zit).
- `tests/e2e/test_lotc_parity.py` — diezelfde vergelijking als poort.
- `tests/e2e/test_lotc_breedte.py` — vangt inhoud die in een te smalle kolom belandt.
- **Wat ze NIET zien:** of een besturingselement echt werkt, of een knop zijn tekst heeft,
  of iets leesbaar is. Daarvoor is stap 4 en 5 nodig.

## Openstaand werk, op volgorde

### 1. Services-pagina: de omgevingsvariabelen terug (ONDERHANDEN)
Het origineel (`opi/templates/services-overview/_diensten.html.j2`) toont per service:
omschrijving, "API naam: `<code>`", en een blok **Omgevingsvariabelen** met per variabele
de naam, de aliassen en de uitleg. De nieuwe kaart toont daarvan niets; de variabelen
werden alleen GETELD op een chip ("3 variabelen").
- `opi/web/lotc_switch.py` levert `variables` nu wel aan (gedaan).
- Nog te doen: `bg/_service-card.html.j2` toont omschrijving, API-naam en het
  variabelenblok. Mag in drie kolommen (verzoek van de gebruiker).

### 2. Projectenpagina: zoeken en sorteren
Gevraagd, nog niet begonnen. Vorm volgens het voorbeeld van de gebruiker: een
`nldd-toolbar` met een zoekveld (start) en een sorteerknop met een uitklapmenu (end).
**Alles via htmx**, zoals de rest van dit project.

### 3. Gebruikersmenu rechtsboven
Uitklapbaar menu met de naam van de gebruiker: profiel, **weergave (systeem/licht/donker)**,
beheer-submenu, uitloggen. NLDD schakelt het thema via `data-scheme` op `<html>`
(`settings.css`); onthouden in localStorage en vóór de eerste weergave zetten, anders
flitst de pagina.

### 4. De e2e-suite groen op de NIEUWE weergave
Gemeten: **39 van de 286 falen** zodra de tests de nieuwe weergave krijgen
(`E2E_LAYOUT=nldd`). Ze wijzen naar de bewerkdialogen, de deployments, backup/restore en
vier wizardstappen. Dit is de belangrijkste post: zolang die tests op de oude weergave
staan, bewaakt niets wat gebruikers echt krijgen.

### 5. Vijf blokken in dialogen tonen nog de oude vormgeving
`FormRenderer._render_layout_element` rendert `TemplatePartial` en `DisplayBlock` ALTIJD
via de roos-omgeving (`opi/forms/renderer.py:917` en `:926`), ongeacht de adapter. Raakt
`modal-edit-domain`, `modal-edit-attachments` en `modal-restore` — en de wizardpagina net
zo goed.

### 6. Knop "Projectgegevens bewerken" ontbreekt
Het origineel heeft hem (`project-details/section-header.html.j2`); op de nieuwe
projectpagina is er geen weg naar `modal-edit-identity`. De pariteitspoort miste dit omdat
hij functieNAMEN vergelijkt en niet hun argumenten — dat is ook een verbeterpunt voor de
meetlat.

### 7. Meetlat bijwerken
- Velden herkennen die als `<nldd-*-field name=...>` renderen (nu ziet hij alleen
  `input/select/textarea` plus drie componentnamen).
- `hx-target` meevergelijken; daardoor kon een kapot doel (`#metrics-content`) aan beide
  kanten blijven staan.
- Argumenten van JS-aanroepen meevergelijken, niet alleen de functienaam.

### 8. Architectuurpagina
1509 regels in één blok. **Als laatste**, op verzoek van de gebruiker.

### 9. De oude weergave eruit
Pas als 4 op nul staat. Dan zijn de oude templates, de schakelaar (`?layout=`, de cookie)
en de oude componentendependency overbodig - en kan het weg omdat het aantoonbaar niet
meer nodig is, in plaats van dat het weggaat en daarna blijkt wat er miste.

## Afspraken die al gemaakt zijn

- Het heet **Services**, niet "Diensten".
- Aanspreekvorm is **je**, niet "u" — ook in teksten die in Python staan
  (`tests/test_lotc_schrijfwijze.py` bewaakt dat).
- **Repositories** hoeft niet getoond te worden.
- Het **resourcegebruik** staat op een eigen tabblad Metrics.
- Blokken die een SERVICE zelf levert renderen via `render_roos()` met hun eigen omgeving:
  zichtbaar anders is beter dan ongemerkt weg.
- De sandbox is van ons; de vergrendeling blijft staan.
