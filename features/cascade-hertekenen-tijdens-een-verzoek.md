# Cascade: hertekenen terwijl er nog een verzoek loopt

## Wat het is

Een afhankelijke keuzelijst in de wizard (een cascade) wordt server-side gevuld: elk veld met
`data-rerender="true"` dient bij een wijziging de hele stap in, en de server tekent de rij
opnieuw met de lijst eronder gevuld. Voorbeeld, cross-domain toegang: kies je een
peer-project, dan verschijnt de lijst met deployments van dat project, en daarna de lijst met
componenten van die deployment.

Dit document beschrijft één regel in dat mechanisme: **een keuze die valt terwijl er nog een
hertekening onderweg is, mag niet verdwijnen.** Zonder die regel deed hij dat, geruisloos.

## Wat er misging

Gemeten in de browser (RC-127), met de gebeurtenissen van htmx erbij:

```
change  to/component   inflight=false  -> htmx:configRequest, htmx:beforeRequest
change  from/project    inflight=true  -> (geen van beide, ook niet later)
beforeSwap, afterRequest, afterSettle
de keuzelijst 'from/deployment' biedt daarna [''] met opschrift "Kies eerst een project"
```

De tweede wijziging leverde dus helemaal geen verzoek op. htmx zet zo'n tweede verzoek in de
wachtrij van het ELEMENT dat het doet - hier het stapformulier - en speelt het na het eerste
antwoord opnieuw af. Maar dat antwoord vervangt `#wizard-step-content`, en het formulier zit
daarbinnen: het haalt zichzelf uit de pagina. htmx weigert een verzoek op een element dat
niet meer in het document staat, en daarmee is de keuze weg zonder fout en zonder herstel.

Voor wie het scherm gebruikt: een geldige keuze in de rij, een lege lijst eronder, en niets
dat nog bijtrekt. Het veld eronder is verplicht, dus de stap is dan niet meer in te vullen.

Met de hand te reproduceren met het toetsenbord - tijdens een verzoek staat `pointer-events`
uit op het formulier, dus met de muis kom je er niet tussen, met het toetsenbord wel - bij
een normale netwerklatentie (gemeten met 400 ms per ronde en 200 ms tussen twee keuzes):
Bron-project sprong terug naar "-- Kies een project --" en Bron-deployment bleef op
"Kies eerst een project" staan.

## Hoe het nu werkt

In `static/js/wizard.js`, in de `change`-luisteraar op `[data-rerender]`:

1. Is het formulier vrij, dan wordt de stap meteen opnieuw ingediend (`_hertekenNu`).
2. Loopt er nog een verzoek, dan wordt de keuze **onthouden** (veldnaam + waarde) en wordt
   gewacht op `htmx:afterSettle` van de swap die landt (`_hertekenNaDeSwap`).
3. Na die swap wordt de waarde teruggezet op het VERSE veld en wordt de wijziging opnieuw
   afgevuurd, wat dan een gewone hertekening oplevert.

Stap 3 is nodig omdat het antwoord dat onderweg was, gerenderd is ZONDER die keuze: de verse
rij komt leeg terug, dus alleen opnieuw indienen zou een leeg veld versturen.

Twee dingen die daarbij bewust zo staan:

- Er wordt gewacht tot het formulier niet meer `htmx-request` draagt. Een antwoord bevat ook
  een OOB-swap van de stapbalk, die vóór de inhoud kan landen; het oude formulier draagt die
  klasse tot na de inhoudsswap, dus zo landen we niet op de verkeerde `afterSettle`.
- Staat de waarde niet meer in de lijst van het verse veld, dan gebeurt er niets en zegt de
  console waarom. Een waarde forceren die de server niet meer aanbiedt, zou een keuze
  versturen die niet bestaat.

## Waar het geldt

De luisteraar is generiek, dus dit geldt voor elk veld met `data-rerender` in de wizard en in
de modal-wizard. Op het moment van schrijven zijn dat er vijftien:

| Waar | Velden |
|---|---|
| cross-domain toegang | Bron-project, Bron-deployment, Mijn component (in- en uitgaand) |
| publish-on-web | domeinformaat, subdomein, basisdomein, eigen basisdomein, TLS, root-component, bare-domain-component |
| deployment | basisdomein, backupschema, deploymentnaam, bijlage-koppeling |
| componenten | gebruikte services |
| keycloak | toegang beperken |

Twee daarvan zijn TEKSTvelden (subdomein, eigen basisdomein), en juist die zitten dicht bij
dit venster: een browser stelt de `change` van een gewijzigd tekstveld uit tot blur of tot het
veld uit de pagina wordt gehaald, dus die landt vaak precies TIJDENS een swap. Dat is in de
meting van de cascade in elke ronde te zien (`change name inflight=true`).

## Toetsing

`operations-manager/python/tests/e2e/test_wizard_cascade_tijdens_verzoek.py` forceert het
venster in plaats van erop te hopen: twee cascadewijzigingen in hetzelfde script, dus de
tweede valt gegarandeerd binnen het verzoek van de eerste. De test toetst dat de
deploymentlijst daarna alsnog gevuld wordt en dat de keuze in de rij staat, en hij eist dat
er ook echt een wijziging binnen een lopend verzoek is gevallen - anders zou hij groen kunnen
staan zonder iets te hebben geraakt.

```bash
uv run pytest tests/e2e/test_wizard_cascade_tijdens_verzoek.py -m "e2e and not sandbox" -q
```

Draai je de bescherming in `static/js/wizard.js` terug, dan blijft de lijst leeg en valt de
test om, met de oorzaak in de melding: het opschrift van de lege optie en de lijst met
overgeslagen wijzigingen.
