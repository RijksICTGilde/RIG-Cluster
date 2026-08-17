# Deployment gezondheid

Een systeemdienst: deze staat altijd aan en is niet iets wat je aanvinkt. Na een uitrol kijkt het platform naar de pods van je deployment en beoordeelt wat het ziet.

## Wat doet het voor je?

- Meldt tijdens het uitrollen waar een component op wacht, in plaats van je te laten raden
- Herkent een container die tegen zijn geheugengrens loopt, blijft herstarten, of waarvan het image niet opgehaald kan worden
- Zet een component stil waarvan het image niet bestaat, zodat het niet eindeloos blijft proberen
- Weet dat een slapende deployment hoort te zwijgen, en meldt dat dan ook zo

## Wat gebeurt er precies?

Er wordt uitsluitend naar de pods van je applicatie gekeken. Een pod die een andere dienst ernaast draait -- de wekker van de slaapstand bijvoorbeeld -- telt niet mee, ook al draagt hij hetzelfde label.

Voordat er een oordeel valt, wordt eerst aan de andere diensten gevraagd wat zij over deze deployment weten. Slaapt hij, dan zijn nul pods de bedoeling en is dat geen storing. Wat wél waargenomen wordt op een draaiende pod blijft altijd een storing: geen enkele dienst kan een echt probleem wegpraten.
