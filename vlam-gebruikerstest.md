#### 1. Verbinden

Installeer indien nodig de Tailscale-client / eigen tools en voer uit:

```
tailscale up --login-server=https://vonk.rijksapp.dev
```

Er opent een browser. Log in met SSO Rijk.

#### 2. Testen

```
curl -v https://chat.rijksweb.nl/health
curl -v https://vlam-api.rijksweb.nl/
```

De eerste hoort `HTTP 200` te geven met `{"status":"pass"}`. Dat is de testbestemming en bewijst dat de hele keten werkt.

De tweede is de echte API. Krijg je een certificaatfout, dan werkt de verbinding wél maar vertrouwt jouw machine de uitgever nog niet: dat certificaat komt van `Rijksdienst Issuing CA2` van SSC-ICT en zit niet standaard in een publieke certificatenlijst. Controleer dat met `curl -k https://vlam-api.rijksweb.nl/`; komt er dan een antwoord, dan is het puur die vertrouwensketen en moet die CA op je machine geïnstalleerd worden.

#### 3. Wat je zou moeten zien

```
tailscale dns status
```

Bij "Split DNS Routes" hoort `rijksweb.nl -> 100.100.100.100` te staan. Al je overige DNS blijft ongemoeid: alleen namen onder `rijksweb.nl` gaan via de tunnel.

#### Als het niet werkt

Laat het in Mattermost weten.