# Verzoeken aan Lord of the Components / NLDD

Wat wij bij het omzetten tegenkwamen en waar het thema (nog) geen antwoord op heeft. Elk
punt is GEMETEN - in de bron van `nldd.js` of in een browser - en niet uit een gevoel dat
er iets zou moeten zijn.

De regel eromheen: zolang iets hier staat, bouwen we het niet zelf na. Wat we intussen
doen staat er per punt bij, en dat is met opzet zo klein mogelijk gehouden.

---

## 1. `nldd-dropdown` tekent zijn label niet bij als het script opties toevoegt

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

## 2. `nldd-sheet` heeft geen publieke "staat hij open"

**Wat er gebeurt.** `show()`, `hide()` en de events `open` en `close` zijn er, maar de
`<dialog>` waar `open` aan af te lezen is, zit in de shadow root.

**Wat wij intussen doen.** Zelf bijhouden in een variabele, met de events als bron.
Dat werkt, maar twee plekken die dezelfde waarheid bewaren lopen ooit uit de pas.

**Voorstel.** Een `open`-property die de `<dialog>` weerspiegelt.

---

## 3. Geen aanduiding voor "de verbinding leeft"

**Wat er ontbreekt.** Een klein statuslampje met een betekenis: verbinden, stromend,
gepauzeerd, fout. `nldd-activity-indicator` is een laadmolen (bezig / klaar) en
`nldd-banner` is een melding; geen van beide is een doorlopende toestand.

**Waar.** De statusregel van het logpaneel.

**Wat wij intussen doen.** Vier eigen regels CSS voor een bolletje van 8 bij 8, met de
kleuren uit de themavariabelen (`--semantics-content-success-color` en verwanten), dus het
volgt licht en donker.

---

## 4. Geen tekstbak waar je regels aan kunt TOEVOEGEN

**Wat er ontbreekt.** `nldd-code-viewer` toont een tekst die je in zijn geheel meegeeft.
Voor een logstroom heb je iets anders nodig: regels die er tijdens het kijken bij komen,
een deel dat verborgen wordt op niveau, en zoektreffers die binnen een regel gemarkeerd
worden.

**Wat wij intussen doen.** Een eigen bak met eigen regelopmaak, in themavariabelen.
Dit is de kandidaat die het minst waarschijnlijk een component wordt, en dat is prima -
het staat hier zodat de volgende niet opnieuw gaat zoeken.

---

## 5. Iconen: de lijst en de bundel lopen uiteen

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

## 6. Samenstellingen met alleen benoemde slots gooien kinderen weg

**Wat er gebeurt.** `<c-toolbar>` en `<c-top-title-bar>` renderen alleen wat in een
benoemde slot staat. Een kind zonder slot verdwijnt zonder melding - ook in de
Jinja-laag, dus je ziet het pas op het scherm.

**Wat wij intussen doen.** Op die plekken kale `nldd-*`-markup schrijven, met
`slot="..."` erop. Dat werkt en het is nog steeds het component van het thema, maar de
`c-`-vorm is dan geen optie meer.

**Voorstel.** Een standaard-slot, of op zijn minst een waarschuwing bij weggegooide
kinderen.
