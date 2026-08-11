# Hoe een aanvinkvakje hoort te werken

Status: plan, 11 augustus 2026. Eén ding uitzoeken, en het dan overal hetzelfde doen.

Aanleiding: het vinkje "Toegang beperken" in de keycloak-configstap is niet uit te zetten. Aanvinken lukt, uitvinken niet. Dit is de derde keer dat een aanvinkvakje terugkomt, en elke keer is er een symptoom gerepareerd. Dat stopt hier: de opdracht is niet "repareer dit vinkje" maar "leg vast hoe een aanvinkvakje werkt", zodat het daarna overal normaal te gebruiken is.

**Geen eigen invoervelden.** Wat het thema levert, gebruiken we. Een kaal `<input>` nabouwen valt af, ook als dat sneller lijkt.

## Wat er nu is, gemeten

**Er staan twee aanpakken naast elkaar**, en ze spreken elkaar tegen:

| | waar | vorm |
|---|---|---|
| enkel vinkje | `widgets/checkbox.html.j2` | `<c-checkbox-field>` |
| groep vinkjes | `widgets/checkbox_group.html.j2` | kaal `<input>` plus `.lotc-checkbox-regel` uit `lotc-app.css` |

De CSS-regel voor die tweede aanpak draagt als reden: *"c-checkbox wordt onder NLDD een webcomponent die zijn invoerveld in de schaduwboom zet"*, dus formulierserialisatie zou hem missen.

De eerste aanpak zegt in zijn eigen commentaar dat dat **opnieuw gemeten is en niet klopt**: het element is form-associated via `ElementInternals` en doet gewoon mee. Er staat zelfs een meting bij (`FormData` leeg voor de klik, gevuld erna).

Twee tegengestelde conclusies over hetzelfde component, allebei met een meting erbij. Dát is het eigenlijke probleem, en waarschijnlijk is er een versie tussen gekomen waarin het gedrag veranderde.

**Een uitgevinkt vakje verstuurt niets.** Dat is normaal HTML-gedrag. `checkbox.html.j2` zet een verborgen tegenhanger, maar alleen bij `readonly`. Wat "afwezig" betekent, beslist de schrijfset: `_may_delete` en `_has_submitted_ancestor` in `opi/forms/wizard/write_set.py` bepalen of ontbreken "leeggemaakt" of "niet meegestuurd" is. Daar zit de kans dat uitvinken stil verdampt.

**Zelfs vinden is onduidelijk.** Een selector op `[id*='restrict-access']` levert een `NLDD-FORM-FIELD-HELP-TEXT` op, niet het vakje. Elk element rond het veld draagt kennelijk die id, en dat maakt elke test en elk script hier broos.

**Het veld zelf:** `KEYCLOAK_RESTRICT_ACCESS_EDITABLE` in `opi/services/catalog/keycloak/editables.py`, met `EmptyToNoneConverter` en `remove_when_none=True`.

## Wat er moet gebeuren

1. **Meet in de browser wat `<c-checkbox-field>` werkelijk doet**, met de versie die we nu draaien. Drie vragen: staat er een echte `<input>` in de lichte boom, in de schaduwboom, of nergens; komt hij in `FormData` als hij aan staat; en wat gebeurt er als hij uit staat. Schrijf de uitkomst op met de versie erbij, want die bepaalt alles hieronder.

2. **Beslis op grond daarvan hoe een vinkje eruitziet**, en doe het dan overal zo. Enkel vinkje en groep horen dezelfde vorm te hebben; nu verschillen ze, en dat is precies hoe deze verwarring ontstond.

3. **Los het uitvinken op bij de bron.** Bepaal wat er hoort te gebeuren als een vakje uit staat, en zorg dat de weg van formulier naar projectbestand dat overbrengt. De verborgen tegenhanger bestaat al voor `readonly`; misschien is dat het antwoord, misschien hoort het in de schrijfset. Kies bewust en schrijf op waarom.

4. **Zorg dat het vakje te vinden is.** Als elk omliggend element dezelfde id draagt, is er geen betrouwbare selector, en dan blijft elke test hier broos. Dat is onderdeel van "normaal kunnen gebruiken".

5. **Leg het vast waar de volgende het zoekt**, met de meting erbij. En haal de twee tegenstrijdige commentaren weg: er mag maar één verhaal over dit component in de codebase staan.

## De toets

Eén test die het hele rondje doet: **aanvinken, opslaan, opnieuw openen, uitvinken, opslaan, opnieuw openen.** Na de tweede opslag hoort het vakje uit te staan en de waarde uit het projectbestand verdwenen te zijn.

Dat is wat er nu misgaat en wat geen enkele bestaande test dekt: alles toetst aanzetten, niets toetst uitzetten.

Draai die test op **beide** vormen (enkel vinkje en groep), want als ze straks dezelfde vorm hebben moeten ze zich ook hetzelfde gedragen.

## Waar op te letten

**Repareer niet alleen "Toegang beperken".** Dat is het symptoom waarmee het gemeld is. Een oplossing die alleen dat veld raakt, laat de volgende drie vinkjes ongemoeid en dan komt dit een vierde keer terug.

**Geen eigen invoerveld.** Als het component het niet kan, is dat de uitkomst: schrijf op wat er precies niet kan en kaart het aan bij het thema. Er omheen bouwen is precies hoe de twee tegenstrijdige aanpakken zijn ontstaan.

**Let op de aanvinkvakjes die JavaScript aanraakt.** `static/js/wizard.js` zoekt met `querySelector('input[type="checkbox"]')` naar het vakje van een dienstkaart. Verandert de vorm, dan verandert dat mee, en die dienstkaarten zijn vandaag al eens omgevallen.
