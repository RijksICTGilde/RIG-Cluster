#### VLAM-API bereikbaar maken voor ontwikkelaars

De VLAM-API staat in het beveiligde RON-netwerk. Onbeheerde laptops kunnen daar niet bij, dus ontwikkelaars die met VLAM willen werken kunnen dat niet. Het reguliere traject via SSC-ICT om dit op te lossen duurt naar verwachting maanden. Wij zoeken een tijdelijke voorziening voor die periode.

#### De oplossing in het kort

Gebruikers komen via een geauthenticeerde VPN uit op een reverse proxy die uitsluitend naar één vastgezet adres kan verbinden. Er bestaat daardoor geen netwerkpad van de gebruiker naar RON. We lussen één API door, we ontsluiten geen netwerk.

```
laptop  ->  VPN  ->  reverse proxy  ->  VLAM-API
            |        |
            |        vaste bestemming, geen andere
            inloggen via SSO Rijk
```

De gebruiker logt in op de VPN via SSO Rijk, met onze Keycloak ertussen. Toegang is beperkt tot mensen met een specifieke rol in die Keycloak; zonder die rol mislukt het inloggen. Toegang is dus per gebruiker te verlenen en in te trekken.

Eenmaal binnen kan de gebruiker precies één adres bereiken: de reverse proxy. Die proxy heeft één vastgezette bestemming, het VLAM-endpoint, en de gebruiker kan die bestemming niet beïnvloeden.

#### De proxy kent maar één route, en die gaat naar de VLAM-API

Een firewallregel is een instelling die verkeerd kan staan of vergeten kan worden, en dan ligt de weg alsnog open. Hier is er geen weg om open te zetten: de proxy kent maar één bestemming, de VLAM API, en de gebruiker kan die niet beïnvloeden.

De proxy zet het verkeer bovendien ongewijzigd door en breekt de versleuteling niet open. Wij hebben geen sleutels en kunnen het verkeer dus niet lezen, ook niet als de proxy zou worden gecompromitteerd.

#### Wat dit niet is

Toegang tot de VLAM-API is iets anders dan toegang tot de gegevens erachter. VLAM geeft zelf API-sleutels uit voor het gebruik van de API. Die autorisatie blijft volledig bij VLAM en staat los van wat wij regelen.

#### Het beveiligingsvraagstuk

RON zit in een beveiligde NORA-zone, en hiermee geven we daar toegang toe. Dat is de kern van de afweging.

Onze inschatting is dat het restrisico vergelijkbaar is met de bestaande situatie op beheerde laptops. Ook daar is SSO Rijk de authenticatie. Wij voegen daar een extra rolcontrole aan toe, dus de groep die binnenkomt is kleiner dan bij beheerde laptops. En waar een beheerde laptop het hele netwerk ziet, is hier één adres bereikbaar. Alle toegang is per persoon te verlenen en in te trekken.

#### Status

Het concept is technisch bewezen op onze productieomgeving: de VPN, de rolcontrole en de vastgezette proxy werken end-to-end.

Of het daadwerkelijk gaat werken kunnen we pas testen zodra de koppeling tussen ODCN en het RON-netwerk voor ons ODI-tenantcluster geregeld is. Die koppeling is aangevraagd.

Deze voorziening is bedoeld als tijdelijk en vervalt zodra SSC-ICT het reguliere pad heeft ingericht.

#### Wat we vragen

We zouden graag akkoord krijgen dat deze opzet voldoet aan de eisen van de BIO en de NORA.

Deze opzet is gemaakt in overleg met het VLAM-team en voldoet aan hun aansluitvoorwaarden.
