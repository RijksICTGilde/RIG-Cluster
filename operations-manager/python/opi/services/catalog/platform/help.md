# Platform

Een systeemdienst: deze staat altijd aan en is niet iets wat je aanvinkt. Elk component krijgt van het platform een paar variabelen die vertellen waar het zichzelf bevindt.

## Wat doet het voor je?

Je applicatie hoeft niet te raden in welke deployment of onder welke naam hij draait. Dat is handig voor logregels, voor foutmeldingen en om per omgeving iets anders te doen zonder daar zelf variabelen voor in te stellen.

## Wat krijg je?

De variabelen **DEPLOYMENT_NAME** (de naam van de deployment waarin je draait) en **COMPONENT_NAME** (de naam van het component). Ze staan in elke pod, zonder dat je iets hoeft te configureren.
