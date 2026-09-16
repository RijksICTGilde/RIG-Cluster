# Eigen container registries

Draait je applicatie op een image die in je eigen private registry staat, dan vul je hier
in waar die registry is en met welk token wij erbij mogen. Daarna kies je bij een component
welke registry bij de image van dat component hoort.

## Wat je invult

- **Naam** - waarmee je bij een component naar deze registry verwijst.
- **Registry** - waar je images staan, inclusief je eigen pad en zonder protocol,
  bijvoorbeeld `code.overheid.nl/jouw-naam`. In kleine letters, en zonder tag: die hoort
  bij de image en niet bij de registry.
- **Gebruikersnaam** en **token** - waarmee wij bij je images mogen.

Je schrijft altijd je eigen registry, nooit een adres van het platform. Wat er technisch
onder gebeurt verschilt per cluster, en dat hoef je niet te weten: je projectbestand blijft
hetzelfde.

## Een publieke image

Dan vink je deze dienst bij dat component niet aan en vul je niets in. Publieke images
werken vanzelf.

De keuze bij een component bepaalt WELKE van je registries voorgaat, niet OF er een geldt.
Staat hierboven `ghcr.io/mijnorg`, dan haalt elk component met een image onder
`ghcr.io/mijnorg` die met jouw token op, ook zonder vinkje bij dat component. Kiezen hoef je
alleen als meer dan een van je registries bij dezelfde image past.

## Wat je van het token moet weten

- Het token heeft **leesrecht op packages** nodig. Een token dat te weinig mag geeft geen
  duidelijke fout maar een melding dat de image niet bestaat, en die wijst de verkeerde
  kant op. Wij toetsen het token daarom bij het opslaan.
- Op productie **verloopt het token na 90 dagen**. Daarna moet je het opnieuw invullen.
- Er zit een **quotum** op wat er van je images wordt bewaard.

## De dienst uitzetten

Zet je de dienst uit, dan wordt de kopie die het platform van je images bewaart opgeruimd,
inclusief de organisatie die daarvoor is aangemaakt. Bovenstrooms, in je eigen registry,
verandert er niets: je images staan er nog. De eerste keer dat een pod daarna start
duurt het pullen iets langer, want de kopie moet opnieuw worden opgehaald.
