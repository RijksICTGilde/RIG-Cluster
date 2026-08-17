# Cross-domain toegang

Standaard mogen projecten elkaars pods niet bereiken. Met deze service benoem je precies welke andere projecten, deployments of componenten bij jou binnen mogen (inbound) en waar jij zelf heen mag (outbound), telkens op een genoemde poort.

## Wanneer gebruik je dit?

- Een ander project moet jouw API rechtstreeks binnen de cluster aanroepen
- Jouw applicatie moet een dienst van een ander project bereiken
- Je hebt verkeer op een andere poort dan 80 of 443 nodig

Gaat het om verkeer via het internet, dan heb je dit niet nodig; dat regelt **Publiceren op het web**. Dit gaat over netwerktoegang tussen projecten in de cluster, niet over DNS-domeinen.

## Wat wordt er ingesteld?

Per deployment wordt een extra NetworkPolicy geschreven die precies de door jou benoemde tegenpartijen openzet, bovenop de standaardregels die cross-tenant verkeer blokkeren.

**De ontvanger beslist.** Een inbound-regel in het project dat benaderd wordt is de toestemming; je kunt jezelf geen toegang geven tot een ander project. Beide kanten leggen dus vast wat ze bedoelen.
