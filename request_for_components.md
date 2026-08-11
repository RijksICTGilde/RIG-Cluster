# Verzoeken aan Lord of the Components / NLDD

Wat wij bij het omzetten tegenkwamen en waar het thema (nog) geen antwoord op heeft. Elk
punt is GEMETEN - in de bron van `nldd.js` of in een browser - en niet uit een gevoel dat
er iets zou moeten zijn.

De regel eromheen: zolang iets hier staat, bouwen we het niet zelf na. Wat we intussen
doen staat er per punt bij, en dat is met opzet zo klein mogelijk gehouden.

---

## 1. De foutmelding bij een formulierveld is onzichtbaar (lotc-forms)

**Wat er gebeurt.** Een `<c-text-input-field error="...">` rendert een
`<nldd-form-field-error-text ... invalid>` met de juiste tekst, en die is in de browser
`display: none` met hoogte 0. Op elk soort veld. De gebruiker ziet een rood kader en
niet wat er mis is; `aria-invalid` staat alleen binnen de schaduwboom, dus voor een
schermlezer is er ook niets.

**Waar het misgaat.** `nldd-form-field._syncErrorText()` bepaalt zelf welke foutregels
zichtbaar zijn:

    const i = veld.hasAttribute("invalid")
    const o = (veld.getAttribute("error-message") ?? "").split(" ")
    regel.toggleAttribute("invalid", i && o.includes(regel.id))

Het leest dus `error-message` OP HET INVOERVELD, en het overschrijft de `invalid` die het
sjabloon op de foutregel zet. `lotc-forms` schrijft daarentegen
`error-message-ids="<id>-error"` op het veld - en dat is de ANDERE richting: die
eigenschap zet `nldd-form-field` zelf, om `aria-describedby` te bedraden. Er komt dus
nooit een id in de lijst die de zichtbaarheid bepaalt.

**Gemeten in chromium, op dezelfde markup met alleen een ander attribuut:**

| markup op het invoerveld | display | hoogte | aria-describedby |
|---|---|---|---|
| `invalid error-message-ids="a-error"` (wat lotc-forms doet) | none | 0 | (leeg) |
| `invalid error-message="b-error"` | block | 18 | b-error |

**Waarom dat pijn doet.** Alles ziet er goed uit: het element staat er, met de goede
tekst, in de goede slot, met `invalid` erop in de bron. Alleen op het scherm staat het
niet. Elke assertie op de HTML is groen.

**Wat wij intussen doen.** Een eigen kopie van `components/_forms.j2` op de searchpath,
waarin `nldd_field` de besturing bedraadt: `error-message-ids` eraf, `invalid`,
`aria-invalid="true"` en `error-message="<id>-error"` erop. Zie
`opi/forms/lotc_attrs.py` (`bedraad_foutmelding`) en
`tests/test_lotc_foutmelding_veld.py`, dat onze kopie naast de geinstalleerde legt zodat
een nieuwe versie van lotc-forms opvalt.

**Voorstel.** In `lotc-forms` `error-message` schrijven in plaats van
`error-message-ids`, en `aria-invalid` op de groepsvelden (radio, aankruisvakjes) zetten
- die hebben geen invoerelement met een schaduwboom die het voor ze doet.

---

## 2. "Optioneel" staat op elk veld dat niet `required` is

**Wat er gebeurt.** `lotc-forms` zet `optional` op elk NLDD-veld dat niet verplicht is
(rijksconventie: markeer optioneel, niet verplicht). Voor een invoerveld klopt dat. Voor
een KIEZER waar altijd iets geselecteerd staat - de deploymentkiezer op de projectpagina,
de rolkeuze bij een uitnodiging - betekent "Optioneel" niets, en bij het enige veld van
een herhaalbaar item ("URI Optioneel") leest het als ruis.

**Wat wij intussen doen.** Een merk-attribuut `data-no-optional-badge` op de besturing,
gelezen door onze kopie van `components/_forms.j2`. De vorige omweg was zulke velden
`required` noemen: het label verdwijnt, maar de HTML zegt dan dat er iets ingevuld MOET
worden - een andere onwaarheid, en een die formuliervalidatie ook echt leest.

**Voorstel.** Een derde stand naast verplicht/optioneel: een veld dat geen van beide
labels draagt. Bijvoorbeeld `optional-label=""` dat het merk weglaat, of een expliciet
`no-optional-badge`.
## 3. `nldd-dropdown` tekent zijn label niet bij als het script opties toevoegt

**Wat er gebeurt.** `<nldd-dropdown>` tekent de gekozen tekst zelf, naast de geslotte
`<select>`. Die tekst wordt bijgewerkt op `slotchange` en op een `change` van de select.
Opties toevoegen aan een select die er al in zit, veroorzaakt geen van beide: `slotchange`
gaat over de toegewezen KNOPEN, en de select zelf verandert niet.

**Wat je ziet.** Een lege keuzelijst terwijl er wel degelijk opties in staan. De lijst
werkt (uitklappen en kiezen gaat goed), hij ZEGT alleen niet wat er gekozen is.

**Waar.** Het logpaneel (`opi/templates_lotc/bg/_log-viewer.html.j2`) vult de
componentkeuze pas als het paneel opengaat: welke componenten er zijn, hangt af van de
deployment waarvan je de logs opvraagt.

**Wat wij intussen doen.** Na het vullen een `change` op de select sturen. Dat is de
gewone DOM-manier om "de keuze is veranderd" te zeggen en het component luistert er al
naar, dus het is geen omweg - maar het is wel iets dat je moet WETEN, en dat is precies
het bezwaar.

**Voorstel.** Een `MutationObserver` op de geslotte select (childList), of een publieke
`sync()`/`refresh()` op het component.

---

## 4. `nldd-sheet` heeft geen publieke "staat hij open"

**Wat er gebeurt.** `show()`, `hide()` en de events `open` en `close` zijn er, maar de
`<dialog>` waar `open` aan af te lezen is, zit in de shadow root.

**Wat wij intussen doen.** Zelf bijhouden in een variabele, met de events als bron.
Dat werkt, maar twee plekken die dezelfde waarheid bewaren lopen ooit uit de pas.

**Voorstel.** Een `open`-property die de `<dialog>` weerspiegelt.

---

## 5. Geen aanduiding voor "de verbinding leeft"

**Wat er ontbreekt.** Een klein statuslampje met een betekenis: verbinden, stromend,
gepauzeerd, fout. `nldd-activity-indicator` is een laadmolen (bezig / klaar) en
`nldd-banner` is een melding; geen van beide is een doorlopende toestand.

**Waar.** De statusregel van het logpaneel.

**Wat wij intussen doen.** Vier eigen regels CSS voor een bolletje van 8 bij 8, met de
kleuren uit de themavariabelen (`--semantics-content-success-color` en verwanten), dus het
volgt licht en donker.

---

## 6. Geen tekstbak waar je regels aan kunt TOEVOEGEN

**Wat er ontbreekt.** `nldd-code-viewer` toont een tekst die je in zijn geheel meegeeft.
Voor een logstroom heb je iets anders nodig: regels die er tijdens het kijken bij komen,
een deel dat verborgen wordt op niveau, en zoektreffers die binnen een regel gemarkeerd
worden.

**Wat wij intussen doen.** Een eigen bak met eigen regelopmaak, in themavariabelen.
Dit is de kandidaat die het minst waarschijnlijk een component wordt, en dat is prima -
het staat hier zodat de volgende niet opnieuw gaat zoeken.

---

## 7. Iconen: de lijst en de bundel lopen uiteen

**Wat er gebeurt.** `icons.json` van `lord_of_the_components` noemt 327 namen; de
`nldd.js` die de browser laadt bevat er 271. De 56 namen ertussen bestaan op papier en
renderen als niets, zonder foutmelding. `media-pause` en `square-arrow-down` zijn er twee
van, en die stonden allebei in onze interface.

**Waarom dat pijn doet.** Een naam die niet bestaat is stil. Wij hadden een test die
precies hierop moest bewaken, en die las de LIJST in plaats van de BUNDEL: hij was
jarenlang groen terwijl er 37 lege plekken in de interface stonden.

**Wat wij intussen doen.** `opi/web/nldd_iconen.py` leest de namen uit de geleverde
bestanden, en `tests/test_lotc_icon_mapping.py` gebruikt die als poort.

**Voorstel.** Of de lijst gelijktrekken met wat er geleverd wordt, of `<nldd-icon>` laten
klagen (console-waarschuwing) bij een naam die hij niet kent.

---

## 8. Samenstellingen met alleen benoemde slots gooien kinderen weg

**Wat er gebeurt.** `<c-toolbar>` en `<c-top-title-bar>` renderen alleen wat in een
benoemde slot staat. Een kind zonder slot verdwijnt zonder melding - ook in de
Jinja-laag, dus je ziet het pas op het scherm.

**Wat wij intussen doen.** Op die plekken kale `nldd-*`-markup schrijven, met
`slot="..."` erop. Dat werkt en het is nog steeds het component van het thema, maar de
`c-`-vorm is dan geen optie meer.

**Voorstel.** Een standaard-slot, of op zijn minst een waarschuwing bij weggegooide
kinderen.

---

## 9. `nldd-list dividers="never"` tekent de lijnen toch

**Wat er gebeurt.** `<nldd-list variant="simple" dividers="never">` ziet er in een
browser precies zo uit als `dividers="always"`: een lijn tussen elke twee regels. Ook
zonder het attribuut. Gemeten met drie lijsten naast elkaar in een pagina.

**Waarom dat pijn doet.** Het attribuut staat in de registry met drie waarden, dus het
sjabloon claimt iets wat niet gebeurt. Dat is erger dan geen attribuut: de volgende
gelooft de markup.

**Wat wij intussen doen.** `dividers` niet meer meegeven, en in de macro opschrijven
waarom.

**Voorstel.** `never` laten doen wat het zegt, of het attribuut uit de registry halen.

---

## 10. Geen stand voor "even hoge kaarten in een rij"

**Wat er gebeurt.** In een `<c-auto-grid>` met een `<c-card>` per cel zijn de CELLEN even
hoog (een grid rekt ze uit) en de KAARTEN niet: die zijn zo hoog als hun inhoud. Een
kaart met een foutmelding erin is dan twee keer zo hoog als zijn buurman, en die buurman
hangt half in de lucht. Gemeten: cel 259px en 259px, kaart 259px en 71px.

**Waarom dat pijn doet.** Het is precies het geval waarvoor je een grid pakt, en de
oplossing (`height: 100%` op de kaart) is een regel CSS die je per plek opnieuw schrijft.

**Wat wij intussen doen.** Een regel in `static/css/lotc-app.css`, gehangen aan de id die
er toch al stond.

**Voorstel.** Een `stretch`-stand op `c-auto-grid`/`c-grid` die zijn kaarten uitrekt, of
een hoogte-attribuut op `c-card`.

---

## 9. `c-bar` zet zijn end-slot in de MIDDELSTE kolom

**Wat er gebeurt.** `.lotc-bar` is een grid met `grid-template-columns: 1fr auto 1fr` en
`justify-self: start/center/end` op de drie regio's. Maar het sjabloon rendert de
middelste `<div class="lotc-bar__center">` alleen `{% if slots.get('center') %}`. Zonder
center-slot heeft het grid dus twee kinderen, en autoplaatsing zet het tweede - het
end-slot - in kolom TWEE (`auto`). `justify-self: end` lijnt het dan uit op de
rechterkant van die middelste kolom.

**Wat je ziet.** "Links dit, rechts dat" wordt "links dit, midden dat". Gemeten op
`/lotc/pagina/admin/users`: balk 992px breed, het end-blok liep van x=781 tot x=914 in
plaats van tot 1344.

**Waarom dat pijn doet.** Dit is precies het geval waarvoor je `c-bar` pakt - een titel
links en een knop rechts - en het faalt STIL: de pagina rendert, alles staat er, het
staat alleen op de verkeerde plek. Geen enkele markupcontrole slaat erop aan.

**Wat wij intussen doen.** `<c-cluster justify="between">`. Dat is flex met
`space-between` en doet precies wat er bedoeld werd.

**Voorstel.** De center-div altijd renderen (leeg is prima, hij is `auto` breed), of het
grid teruggeven op `1fr auto` als er geen center-slot is.

---

## 10. `c-avatar-group` rendert een element dat NLDD niet kent

**Wat er gebeurt.** `<c-avatar-group>` rendert `<nldd-avatar-group>`, en dat custom
element wordt door de meegeleverde `nldd.js` niet gedefinieerd. Gemeten in de browser:
het bleef als enige over in
`document.querySelectorAll('*:not(:defined)')`.

**Wat je ziet.** Niets bijzonders - en dat is het probleem. Een niet-gedefinieerd custom
element geeft geen fout en verdwijnt niet; het is een inline wrapper die niets doet. De
avatars erin blijven staan, maar de groepering en het "+N" dat de groep zelf zou tellen
komen er nooit. Je ziet alle vier de bolletjes naast elkaar in plaats van drie plus een
telbol.

**Waar.** Het projectoverzicht, de teamkolom.

**Wat wij intussen doen.** `<c-avatar>` in een `<c-cluster>`, en het aftellen naar "+N"
met de hand in het sjabloon.

**Voorstel.** Het element registreren in de bundel, of - als het bewust niet meekomt -
`avatar-group` uit de registry halen, zodat het als niet-geimplementeerd component
opvalt in plaats van stil niets te doen.

---

## 11. `c-code-viewer` toont geen tekst die er later in gezet wordt

**Wat er gebeurt.** `<c-code-viewer>` is een CodeMirror-weergave die zijn inhoud
overneemt bij het opbouwen. Zet je de tekst er daarna in met `.textContent`, dan gebeurt
er niets.

**Wat je ziet.** Een leeg blok. Gemeten op de toolspagina: met
`<c-code-viewer id="output-text">` en `outputText.textContent = '...'` bleef de zichtbare
tekst leeg; met een gewone `<pre>` op dezelfde plek staat hij er.

**Waarom dat pijn doet.** "Een blok code met een kopieerknop" is precies waar je dit
component voor pakt, en het halve gebruik ervan is een resultaat dat pas NA een
serververzoek binnenkomt.

**Waar.** De toolspagina (`opi/templates_lotc/tools.html.j2`), het resultaat van
encrypt/decrypt. Het logpaneel liep tegen dezelfde grens aan, om een andere reden
(regels die er een voor een bij komen).

**Wat wij intussen doen.** Een eigen `<pre>` met een klasse in `static/css/tools.css`.

**Voorstel.** De inhoud volgen met een `MutationObserver`, of een publieke
`setValue()`/`value` op het component.
