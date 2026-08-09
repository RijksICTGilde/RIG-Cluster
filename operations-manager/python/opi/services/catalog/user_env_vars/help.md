# Eigen omgevingsvariabelen

Een systeemdienst: deze staat altijd aan en is niet iets wat je aanvinkt. Elk component heeft eigen omgevingsvariabelen, en hier stel je ze in.

## Wat doet het voor je?

Je geeft je applicatie instellingen mee zonder je container-image aan te passen: een logniveau, een feature-vlag, het adres van een externe koppeling of een sleutel van een andere partij. De waarden worden versleuteld opgeslagen, dus ook een wachtwoord kan hier.

## Waar let je op?

Je schrijft ze als **NAAM=waarde** of als YAML. Je kunt naar platformvariabelen verwijzen met **$DATABASE_SERVER_HOST**-achtige verwijzingen; een verwijzing die niet bestaat blijft hier gewoon staan, want een dollarteken in een wachtwoord is geen typefout.

Je kunt ze per component instellen en per deployment overschrijven, zodat een testomgeving andere waarden kan hebben dan productie.

## Waar staan deze variabelen in de API?

De variabelen die hier gezet worden, staan **niet** bij de **variables** van de andere diensten: die lijst is wat het platform zelf levert. Wie wil weten wat er uiteindelijk in een container staat, moet daar de eigen omgevingsvariabelen van dit component en de **aliassen** bij optellen. Ze worden per component beheerd via **/api/v2/projects/{project}/services/user-env-vars/values/component/{component}**.
