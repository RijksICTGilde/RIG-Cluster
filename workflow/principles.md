# Vaste regels

Deze regels gelden ALTIJD, voor elke rol: bouwer, reviewer, security. Ze staan hier
omdat ze anders per taak als correctie moeten worden gegeven, en dat kost elke keer een
volle ronde. Ze zijn geen suggestie.

Twee van de regels hieronder zijn tijdens RC-177 met de hand ingestuurd, in ronde 3 en
ronde 10. Dat is precies wat dit bestand voorkomt.

## Vorm volgen, niet zelf verzinnen

Bouw zoals dit project het al doet. Voordat je een nieuw patroon neerzet, kijk hoe een
bestaand pakket het oplost en volg dat.

- `instructions/services.md` beschrijft hoe een dienst is opgebouwd. Dat is de vorm.
- Kijk naar bestaande dienstpakketten voordat je begint: metrics-scraper voor
  componentconfig, auth-wall voor projectconfig.
- Editables zijn het startpunt van een dienst: yaml-pad, validators en converters horen
  daar. Niet in de vorm, niet in een endpoint.
- Formulier en API lopen hetzelfde logicapad. Geen tweede route die net iets anders doet.

## Eerst zoeken, dan schrijven

Voordat je een helper schrijft, zoek of hij bestaat. Een tweede exemplaar naast een
bestaande oplossing is duurder dan een aanroep die net niet lekker past.

- Eén lezer per veld. Wordt een waarde op meerdere plekken uitgepakt, gevalideerd of
  genormaliseerd, dan hoort dat via één gedeelde functie te lopen.
- Concreet gemeten op RC-177: een eigen uitpakker naast `decrypt_password_smart`, de
  sleutelresolver nagebouwd in de formulierlaag, een eigen URL-normalisatie, en dezelfde
  hulpfunctie drie keer in één pakket.
- Verhuis je een sleutel of veld, verhuis dan al zijn bewakers mee. Schemavalidatie,
  eigendomscontrole en normalisatie horen bij elkaar te blijven.

## Minimale uitleg in de code

Een commentaar of docstring zegt kort WAT iets doet, en alleen als dat niet al uit de
code volgt. Geen essays boven een functie, geen herhaling van wat de regel eronder zegt.

Comply or explain: leg een afwijking van een afspraak uit, maar herhaal nooit wat de
code zelf al zegt.

## Schrijfwijze

- Nooit em dashes, niet in code, niet in commentaar, niet in commitberichten en niet in
  PR-teksten. Gebruik een komma, een dubbele punt, haakjes, of splits de zin.
- Commitberichten en PR-teksten noemen het gereedschap niet. Geen "Claude", geen
  "Anthropic", geen "AI", en geen trailer met een van die woorden. Een commitbericht
  beschrijft de wijziging, niet wie hem maakte.

## Chirurgisch wijzigen

Raak aan wat de taak vraagt. Niet en passant hernoemen, herindelen of "verbeteren" wat
er al stond. Zie je iets dat echt kapot is buiten de opdracht, repareer het en meld het
apart op de PR.
