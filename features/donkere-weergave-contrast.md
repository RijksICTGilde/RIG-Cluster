# De donkere weergave: waar de kleuren vandaan komen, en wat er mis kan gaan

De gebruiker kiest licht of donker via `/weergave`; die keuze staat in het koekje
`zad_scheme` en `base_lotc.html.j2` zet hem als `data-scheme` op `<html>`, nog vóór de
eerste weergave. Vanaf daar doet het NLDD-thema de rest: zijn tokens zijn
`light-dark()`-waarden en `color-scheme` laat de browser ook zijn eigen vlakken (het
paginavlak, formuliervelden, scrollbalken) in de goede stand tekenen.

Alles wat zijn kleur uit een themotoken haalt, gaat dus vanzelf mee. Deze pagina gaat over
wat dat niet doet.

## De ene fout die dit steeds veroorzaakt

Een kleur die niet meebeweegt, terwijl de kleur ernaast dat wel doet. Twee vormen:

```css
/* 1. een vaste waarde */
background: #FFFFFF;

/* 2. een variabele die NERGENS gezet wordt, met een vaste kleur als terugval */
background: var(--nldd-color-surface, #fff);
```

De tweede is de gemene: hij ziet eruit als een themawaarde. Maar als de naam nergens
bestaat, neemt de browser altijd de terugval - en die is per definitie vast. In de lichte
weergave klopt het toevallig, want de terugval is voor licht gekozen. In de donkere
weergave krijg je lichte tekst op een wit vlak, of donkere tekst op een donker vlak.

Zo zag RC-134 eruit, gemeten in Chromium:

| plek | kleur | achtergrond | contrast | herkomst |
|---|---|---|---|---|
| projectpagina, "Configuratie & Secrets" | #FFFFFF | #FFFFFF | 1,00 | `c-secret-field` (componentenlaag) |
| bewerkdialoog: kop, labels, "Annuleren" | #D9DEE5 | #FFFFFF | 1,35 | `modal.css` (van ons) |
| bewerkdialoog: "Laden..." | #64748B | #FFFFFF | 3,24 | `modal.css` (van ons) |
| `/introductie`, "Naar zad-cli" | #154273 | #121212 | 1,84 | `.lotc-shortcut-cta` (componentenlaag) |
| `/admin/usage`, de filterlabels | #1A1A1A | #121212 | 1,08 | `--nldd-color-text` zonder invulling |

De norm is WCAG AA: 4,5:1 voor gewone tekst, 3:1 voor grote tekst (>= 24px, of >= 18,66px
en vet).

## Hoe je het repareert

**Gebruik de themawaarde.** Niet een tweede vaste kleur voor donker ernaast zetten: dat is
een eigen kleurenstelsel bouwen, en dan heb je twee stelsels die uit elkaar gaan lopen.

De tokens die je bijna altijd nodig hebt:

| waarvoor | token | licht | donker |
|---|---|---|---|
| een vlak (kaart, dialoog, veld) | `--semantics-surfaces-base-background-color` | #FFFFFF | #20252B |
| gewone tekst | `--semantics-content-color` | | |
| gedempte tekst | `--semantics-content-secondary-color` | | |
| een lijn of rand | `--semantics-dividers-color` | | |
| accent-/linktekst | `--semantics-content-accent-color` | | |
| fouttekst | `--semantics-content-critical-color` | | |

Een vaste waarde als terugval ACHTER zo'n token mag: `var(--semantics-…, #fff)`. Die
terugval wordt nooit gebruikt zolang het thema geladen is, en is er voor het geval dat niet
zo is. Wat niet mag, is een terugval achter een naam die nergens bestaat.

**De namen die de componentenlaag opvraagt, vullen wij in.** Een handvol handgeschreven
componenten (`c-secret-field`, `c-data-list`, het `select-field` van lotc-forms) schrijft
`var(--nldd-color-…, <vast>)`. Die namen bestaan in het thema niet. `static/css/lotc-app.css`
zet ze op `:root` uit de themawaarden hierboven, zodat die componenten meebewegen zonder dat
wij hun opmaak overschrijven - dezelfde weg als het blok voor `nldd-sidebar-section`
daaronder. Het staat als verzoek in `request_for_components.md` en kan weg zodra die
componenten hun kleuren zelf uit `--semantics-…` halen.

Onze eigen regels die diezelfde `--nldd-color-…`-namen spiegelen
(`bg/admin-usage.html.j2`, `metrics-charts.css`) volgen daar automatisch in mee - dat was
de bedoeling van dat spiegelen.

## De twee poorten

**`tests/test_donkere_weergave_vaste_kleuren.py`** (bron, snel). Zoekt in onze stylesheets,
onze sjablonen en de componentenlaag naar `var(--naam, <kleur>)` waarvan `--naam` nergens
gezet wordt. Dat is precies de fout hierboven, en de test noemt bestand en regel. Namen uit
de componentenlaag die wij bewust niet invullen staan in `BEWUST_NIET_INGEVULD`, met de
reden: die componenten zetten hun voorgrond én achtergrond zelf vast, dus zijn ze in beide
standen even leesbaar. Een tweede test houdt die lijst schoon: een uitzondering die geen
enkel component meer opvraagt, hoort weg.

**`tests/e2e/test_donkere_weergave_contrast.py`** (browser, echt). Rekent per stuk tekst op
de getroffen schermen de werkelijke verhouding uit, in beide standen. Twee dingen daarin
zijn niet vanzelfsprekend en kosten anders een uur zoeken:

- **De achtergrond komt van `background-color: Canvas`, niet van `<html>`.** Met
  `color-scheme: dark` tekent de browser zelf een donker paginavlak (#121212 in Chromium),
  terwijl de computed `background-color` van `<html>` doorzichtig blijft. Wie omhoog loopt
  tot hij iets tegenkomt, eindigt op wit en meet dan contrast 1,00 op élk element van de
  pagina - ruis waarin de echte fouten wegvallen.
- **Kleuren gaan door een canvas en niet door een reguliere expressie.** Het thema levert
  zijn kleuren als `oklch(...)`. Een rgb-parser slaat die stilletjes over, en dan meet je
  een fractie van de pagina zonder dat iets rood wordt. Verven en de pixel teruglezen werkt
  voor elke notatie.

Het omhooglopen gaat bovendien door de PLATGESLAGEN boom (`assignedSlot`, dan
`parentElement`, dan de schaduwgastheer): een geslot element hangt onder zijn `<slot>` en
niet onder zijn eigen ouder, en de vlakken zitten juist in die schaduwboom.

## Wat NIET stuk was

De melding noemde ook het voortgangsscherm van een taak (de stappenlijst en de kop
"Voortgang"). Dat scherm is volledig van thema-componenten gemaakt en meet in de donkere
weergave 15,4:1 tot 18,7:1. Er viel daar niets te repareren; het staat in de suite mee,
zodat die vaststelling houdbaar blijft.
