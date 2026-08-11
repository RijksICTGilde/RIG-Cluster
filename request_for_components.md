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
