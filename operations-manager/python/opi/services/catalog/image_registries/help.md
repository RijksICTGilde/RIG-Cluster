# Eigen container registries

Draait je applicatie op een image die in je eigen private registry staat, dan vul je hier in waar die registry is en met welk token wij erbij mogen. Daarna staat er bij elk component een keuzeveld **Registry**, direct onder het image-veld, waar je zegt waar die image vandaan komt.

## Wat je invult

- **Naam** - hoe jij deze registry noemt, bijvoorbeeld `Code Overheid`. Vrije tekst; wij maken er zelf een korte verwijzing van.
- **Registry** - waar je images staan, inclusief je eigen pad en zonder protocol, bijvoorbeeld `code.overheid.nl/jouw-naam`. Heb je de pagina van je packages open, dan mag je die URL hier ook gewoon plakken: wij maken er de goede vorm van. Datzelfde geldt voor een volledige image-verwijzing met tag.
- **Token** - waarmee wij bij je images mogen. Dit veld is verplicht: zonder token kunnen we je images niet ophalen.
- **Gebruikersnaam** - alleen nodig bij een registry die ernaar kijkt. Bij Docker Hub en Quay is dat zo: vul daar je accountnaam of de naam van je robotaccount in. Bij GitHub (ghcr.io) maakt de waarde niet uit en mag je hem leeg laten; wij vullen dan zelf iets in waar je token mee werkt, en in je projectbestand blijft het veld leeg. Eist je registry toch een echte naam en laat je hem leeg, dan lukt het ophalen van je images niet. Wij controleren je token bij het opslaan van dit blok, maar alleen als er al een component is met een image uit deze registry; voeg je de registry eerst toe, sla het blok dan opnieuw op zodra dat component er staat.

Je schrijft altijd je eigen registry, nooit een adres van het platform. Wat er technisch onder gebeurt verschilt per cluster, en dat hoef je niet te weten: je projectbestand blijft hetzelfde.

De naam die je kiest ligt vast zodra de registry bestaat, ook als je het label later verandert: componenten verwijzen ernaar en hij zit in de naam van het pull-secret.

## Een publieke image

Dan laat je het keuzeveld bij dat component op **Publieke registry, geen token nodig** staan. Dat is de standaard, en er wordt niets opgeslagen. Publieke images werken vanzelf.

Heeft je project nog geen enkele registry, dan is dat keuzeveld er ook niet: er valt dan niets te kiezen. Je eerste registry voeg je dus hier toe, in dit blok.

De keuze bij een component bepaalt WELKE van je registries voorgaat, niet OF er een geldt. Staat hierboven `ghcr.io/mijnorg`, dan haalt elk component met een image onder `ghcr.io/mijnorg` die met jouw token op, ook zonder vinkje bij dat component. Kiezen hoef je alleen als meer dan een van je registries bij dezelfde image past.

## Wat je van het token moet weten

- Het token heeft **leesrecht op packages** nodig. Een token dat te weinig mag geeft geen duidelijke fout maar een melding dat de image niet bestaat, en die wijst de verkeerde kant op. Wij toetsen het token daarom bij het opslaan.
- Op productie **verloopt het token na 90 dagen**. Daarna moet je het opnieuw invullen.
- Er zit een **quotum** op wat er van je images wordt bewaard.

## Een registry weghalen

Dat kan pas als geen enkel component hem meer gebruikt. Anders weigeren we het opslaan en noemen we de componenten erbij die hem nog aanwijzen. We ruimen ze niet zelf op: dan zou stilletjes veranderen waar een image vandaan komt, en daar hoort iemand bij na te denken.

## De dienst uitzetten

Net als bij een losse registry kan dat pas als geen enkel component er nog een van je registries bij staan heeft: anders weigeren we het opslaan en noemen we de componenten erbij. Zet die dus eerst op de publieke registry.

Zet je de dienst uit, dan wordt de kopie die het platform van je images bewaart opgeruimd, inclusief de organisatie die daarvoor is aangemaakt. Bovenstrooms, in je eigen registry, verandert er niets: je images staan er nog. De eerste keer dat een pod daarna start duurt het pullen iets langer, want de kopie moet opnieuw worden opgehaald.
