# Aliassen

Een systeemdienst: deze staat altijd aan en is niet iets wat je aanvinkt. Een alias geeft een platformvariabele een tweede naam, namelijk de naam die jouw applicatie verwacht.

## Wat doet het voor je?

Verwacht je applicatie **POSTGRES_HOST** terwijl het platform **DATABASE_SERVER_HOST** levert, dan schrijf je **POSTGRES_HOST: $DATABASE_SERVER_HOST**. Je hoeft je applicatie of je image dus niet aan te passen aan de namen van het platform.

## Waar let je op?

Een alias die naar een variabele verwijst die niet bestaat is hier een **harde fout**: het uitrollen stopt. Dat is anders dan bij een eigen omgevingsvariabele, waar zo'n verwijzing blijft staan. Een alias is per slot van rekening altijd bedoeld als verwijzing.

## Waar staan deze variabelen in de API?

Een alias is een extra naam die **niet** bij de **variables** van de dienst staat waar hij naar verwijst: die lijst is wat het platform zelf levert onder zijn eigen namen. Wie wil weten wat er uiteindelijk in een container staat, moet daar de aliassen en de **eigen omgevingsvariabelen** van dit component bij optellen. Ze worden per component beheerd via **/api/v2/projects/{project}/services/aliases/values/component/{component}**.
