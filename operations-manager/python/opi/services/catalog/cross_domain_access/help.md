# Cross-domain toegang

Standaard mogen pods van verschillende deployments elkaar niet bereiken, ook niet binnen hetzelfde project. Met deze service benoem je precies welke deployments en componenten bij jou binnen mogen (inbound) en waar jij zelf heen mag (outbound), telkens op een genoemde poort.

## Wanneer gebruik je dit?

- Een ander project moet jouw API rechtstreeks binnen de cluster aanroepen
- Jouw applicatie moet een dienst van een ander project bereiken
- Twee deployments van jouw eigen project moeten elkaar bereiken
- Je hebt verkeer op een andere poort dan 80 of 443 nodig

De afscherming zit op deployment-niveau, niet op projectniveau. Wil je vanuit deployment `test` naar deployment `acceptatie` van hetzelfde project, kies dan gewoon je eigen project als tegenpartij. Componenten binnen één deployment mogen elkaar altijd al bereiken; daar heb je hier niets voor nodig.

Gaat het om verkeer via het internet, dan heb je dit niet nodig; dat regelt **Publiceren op het web**. Dit gaat over netwerktoegang tussen projecten in de cluster, niet over DNS-domeinen.

## Wat wordt er ingesteld?

Per deployment wordt een extra NetworkPolicy geschreven die precies de door jou benoemde tegenpartijen openzet, bovenop de standaardregels die cross-tenant verkeer blokkeren.

**De ontvanger beslist.** Een inbound-regel in het project dat benaderd wordt is de toestemming; je kunt jezelf geen toegang geven tot een ander project. Beide kanten leggen dus vast wat ze bedoelen.

**Een gedeelde voorziening zonder projectlimiet.** Draai je iets waar in principe elk project bij mag -- een gedeelde API binnen de cluster -- dan is een regel per afnemer bijhouden geen toegangsbeleid maar een wachtlijst. Voor dat geval kan een inbound-regel `*` als bron-project dragen: die ene poort van dat ene component staat dan open voor elke bron, en `deployment` en `component` van de bron laat je leeg. De keuzelijst biedt dit niet aan; je zet het via de API of het projectbestand, want het is een besluit over een voorziening en niet over een tegenpartij. Denk er wel om dat je applicatie dan zelf haar bellers moet herkennen, bijvoorbeeld met een sleutel: de netwerkregel zegt vanaf dat moment alleen nog wie er bij kan, niet wie het is.

Blijft ook gelden binnen je eigen project: je hebt dan twee regels nodig, een outbound bij de bellende kant en een inbound bij de gebelde kant. Ze staan allebei in hetzelfde projectbestand, dus dat is één bewerking, maar één van de twee is niet genoeg.
