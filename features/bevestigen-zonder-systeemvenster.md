# Bevestigen zonder systeemvenster

Elke vraag die het portaal stelt voordat het iets doet, valt in een dialoog van het thema.
Geen `confirm()`, geen `alert()`, geen `hx-confirm`. Een systeemvenster van de browser
draagt de naam van de host, weet niets van de vormgeving, bevriest de pagina tot je klikt,
en is niet te stijlen en niet te testen.

Voor een korte vraag bij een knop is dit de weg. Voor een onomkeerbare actie op de
projectpagina, waar de server het endpoint zelf moet afleiden en de vraag de lopende taak
wordt, is er de server-side bevestiging uit
[project-action-confirmations](project-action-confirmations.md).

## Gebruik

Zet drie data-attributen op het formulier dat om een bevestiging vraagt:

```jinja
<form method="post" action="/admin/users/{{ user.id }}/delete"
      data-bevestig="{{ user.full_name }} verwijderen?"
      data-bevestig-uitleg="Daarna kan {{ user.full_name }} niet meer inloggen op het portaal."
      data-bevestig-knop="Gebruiker verwijderen">
```

En zorg dat de pagina de dialoog meeneemt:

```jinja
{% include "bg/_bevestiging.html.j2" %}
```

Dat is alles. Het werkt voor een gewoon formulier met een redirect erachter en voor een
formulier dat htmx verstuurt (`hx-post`).

De teksten: vraag wat er gebeurt en aan wie, met de naam erin, en geef de knop het
werkwoord ("Token intrekken") in plaats van "Doorgaan". De uitleg zegt wat er daarna anders
is. Bij een onomkeerbare actie staat de focus op Annuleren, en Escape sluit de vraag zonder
iets te doen en zonder iets anders te raken.

Een vraag vanuit eigen JavaScript stelt dezelfde dialoog rechtstreeks:

```js
window.vraagBevestiging(vraag, uitleg, knoplabel, functieDieHetDoet);
```

## Hoe het werkt

`bg/_bevestiging.html.j2` zet een lege `<c-modal-dialog variant="destructive">` op de
pagina en laadt `static/js/bevestiging.js`. Dat script vult de vraag, de uitleg en het
label van de knop, en opent de dialoog. De handeling loopt pas na een klik op de
bevestigknop.

- **Een htmx-formulier** gaat langs het `htmx:confirm`-event, dat htmx voor elk verzoek
  afgeeft: het verzoek wordt tegengehouden en na een bevestiging vrijgegeven met
  `issueRequest(true)`.
- **Een gewoon formulier** wordt in de capture-fase van `submit` tegengehouden en na een
  bevestiging opnieuw verstuurd.

### Waarom geen hx-confirm

htmx heeft er een attribuut voor, en dat valt terug op `window.confirm` zodra het script
niet laadt. Dan is het systeemvenster stilletjes terug, op het moment dat er al iets mis is.
Vandaar het event en niet het attribuut.

### De POST-bestemming komt nooit uit een verzoek

De bevestiging verandert niets aan waar het formulier naartoe post: dat blijft het
`action`/`hx-post` uit de server-side markup. Er is dus geen endpoint dat uit een
querystring komt, en dat is de eis bij een actie die een gebruiker of een service account
verwijdert.

De dialoog is daarbij een gemak en geen poort. Wie hem omzeilt en rechtstreeks post, loopt
nog steeds tegen dezelfde autorisatie, CSRF en (bij tokens) step-up aan. Verplaats daar dus
geen controle naartoe.

### Boven een open bewerkmodal

`<nldd-modal-dialog>` draait op een echte `<dialog>` met `showModal()`, dus hij komt in de
toplaag van de browser. De gedeelde bewerkmodal is een gewone div met een z-index, en de
toplaag ligt daarboven, ongeacht die z-index. Een bevestiging vanuit een open bewerkmodal
kan dus gewoon, en dat is wat de waarschuwing bij niet-opgeslagen werk doet.

Escape handelt het script zelf af, in de capture-fase op `document`: het sluit alleen de
vraag. De browser zou de `<dialog>` ook zelf sluiten, maar die keydown bubbelt uit de
toplaag door naar `document`, en daar sluit `edit_modal.js` elke open bewerkmodal. Escape
gooide daarmee precies het niet-opgeslagen werk weg waar de vraag over gaat.

Die afhandeling moet weg zodra de vraag weg is, hoe hij ook sluit. De component sluit namelijk
ook zelf, op een klik naast de dialoog, en dan komt er geen klik op een van de twee knoppen.
Het script ruimt daarom ook op het `close`-event op dat de component op zijn hostelement
afgeeft, en laat Escape door zodra de dialoog niet meer in de pagina staat (een htmx-swap).
Zonder die opruiming zou de volgende Escape niet werken en zou de bewerkmodal pas op de tweede dicht gaan.

Een pagina die de bewerkmodal monteert, neemt daarom ook deze dialoog mee: `bg/_modals.html.j2`
doet dat voor de projectpagina, `bg/admin-approvals.html.j2` voor de goedkeuringen. Zonder
de dialoog zou de waarschuwing daar op een ontbrekende functie stuiten.

## Waar het staat

| Plek | Vraag |
|---|---|
| `bg/_tokens.html.j2` | een token intrekken, een service account verwijderen |
| `bg/admin-users.html.j2` | sessies beëindigen, een gebruiker verwijderen, een 2-factor leegmaken |
| `static/js/edit_modal.js` | een bewerking met niet-opgeslagen wijzigingen sluiten |

## Tests

- `tests/test_bevestigingen.py` -- de poort: `confirm(`, `hx-confirm` en `alert(` komen niet
  voor in `opi/templates_lotc/` of `static/js/`. Commentaar gaat eerst weg, zodat een
  toelichting mag vertellen wat er stond. De twee uitzonderingen staan er met hun reden, en
  een tweede toets faalt zodra zo'n reden verdwijnt. Een derde eist dat elke pagina met
  `data-bevestig` in zijn boom ook `bg/_bevestiging.html.j2` insluit: zonder de dialoog post
  de knop zonder vraag.
- `tests/e2e/test_bevestigingsdialoog.py` -- een token intrekken en een bewerking met
  wijzigingen sluiten, inclusief de dialoog boven de open bewerkmodal, Escape erop, een
  vraag die naast de dialoog wordt weggeklikt waarna Escape de bewerking wel sluit, en een
  vraag waarvan de dialoog uit de pagina gaat waarna Escape doorloopt.
- `tests/e2e/test_user_admin.py::TestBevestigingMetVijandigeNaam` -- de drie handelingen van
  gebruikersbeheer, met een vijandige naam.

Elke browsertoets registreert `page.on("dialog", ...)` en faalt als dat event ooit afgaat.
Dat is de meting die zegt dat er geen systeemvenster meer komt.
