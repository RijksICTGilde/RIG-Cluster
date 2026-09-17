# Eigen container registries

Draait je applicatie op een image die in je eigen private registry staat, dan vul je hier in waar die registry is en met welk token wij erbij mogen. Daarna staat er bij elk component een keuzeveld **Registry**, direct onder het image-veld, waar je zegt waar die image vandaan komt.

## Wat je invult

- **Naam** - hoe jij deze registry noemt, bijvoorbeeld `Code Overheid`. Vrije tekst; wij maken er zelf een korte verwijzing van, en die blijft hetzelfde als je het label later verandert.
- **Registry** - waar je images staan, inclusief je eigen pad en zonder protocol, bijvoorbeeld `code.overheid.nl/jouw-naam`. Heb je de pagina van je packages open, dan mag je die URL hier ook plakken: wij maken er de goede vorm van. Datzelfde geldt voor een volledige image-verwijzing met tag.
- **Token** - waarmee wij bij je images mogen. Verplicht.
- **Gebruikersnaam** - alleen nodig bij een registry die ernaar kijkt, zoals Docker Hub en Quay: vul daar je accountnaam of de naam van je robotaccount in. Bij GitHub (ghcr.io) mag je hem leeg laten. Eist je registry een naam en laat je hem leeg, dan lukt het ophalen van je images niet.

Je schrijft altijd je eigen registry, nooit een adres van het platform. Wat er technisch onder gebeurt verschilt per cluster, en dat hoef je niet te weten: je projectbestand blijft hetzelfde.

## Een publieke image

Dan laat je het keuzeveld bij dat component op **Automatisch** staan. Dat is de standaard, en er wordt niets opgeslagen. Publieke images werken vanzelf.

Heeft je project nog geen registry, dan staat dat keuzeveld er niet. Je eerste registry voeg je dus hier toe.

Automatisch is dus niet altijd publiek: de keuze bij een component bepaalt WELKE van je registries voorgaat, niet OF er een geldt. Staat hierboven `ghcr.io/mijnorg`, dan haalt elk component met een image onder `ghcr.io/mijnorg` die met jouw token op, ook als het op Automatisch staat. Kiezen hoef je alleen als meer dan een van je registries bij dezelfde image past.

## Wat je van het token moet weten

- Het token heeft **leesrecht op packages** nodig. Een token dat te weinig mag geeft geen duidelijke fout maar een melding dat de image niet bestaat, en die wijst de verkeerde kant op. Wij toetsen het token daarom bij het afronden van de wizard en bij het opslaan van dit blok, tegen de images die je componenten dan al hebben. Voeg je daarna via het componentformulier een component met een image uit deze registry toe, sla dit blok dan nog eens op.
- Op productie **verloopt het token na 90 dagen**. Daarna moet je het opnieuw invullen.
- Er zit een **quotum** op wat er van je images wordt bewaard.

## Een registry weghalen

Dat kan pas als geen enkel component hem meer gebruikt. Anders weigeren we het opslaan en noemen we de componenten erbij die hem nog aanwijzen. We ruimen ze niet zelf op, want dan verandert stilletjes waar een image vandaan komt.

## De dienst uitzetten

Net als bij het weghalen van een registry kan dat pas als geen component er nog een gebruikt. Zet die componenten eerst op Automatisch.

Zet je de dienst uit, dan wordt de kopie die het platform van je images bewaart opgeruimd, inclusief de organisatie die daarvoor is aangemaakt. Bovenstrooms, in je eigen registry, verandert er niets: je images staan er nog. De eerste keer dat een pod daarna start duurt het pullen iets langer, want de kopie moet opnieuw worden opgehaald.
