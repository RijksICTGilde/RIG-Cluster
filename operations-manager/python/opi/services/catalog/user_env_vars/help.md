# Eigen omgevingsvariabelen

Een systeemdienst: deze staat altijd aan en is niet iets wat je aanvinkt. Elk component heeft eigen omgevingsvariabelen, en hier stel je ze in.

## Wat doet het voor je?

Je geeft je applicatie instellingen mee zonder je container-image aan te passen: een logniveau, een feature-vlag, het adres van een externe koppeling of een sleutel van een andere partij. De waarden worden versleuteld opgeslagen, dus ook een wachtwoord kan hier.

## Waar let je op?

Je schrijft ze als **NAAM=waarde** of als YAML. Je kunt naar platformvariabelen verwijzen met **$DATABASE_SERVER_HOST**-achtige verwijzingen; een verwijzing die niet bestaat blijft gewoon staan.

Je kunt ze per component instellen en per deployment overschrijven.
