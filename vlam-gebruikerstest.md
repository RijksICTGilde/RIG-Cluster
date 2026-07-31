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

#### 4. Het certificaat eenmalig vertrouwen

Het certificaat van `vlam-api.rijksweb.nl` komt van een eigen PKI van SSC-ICT en niet van een publieke uitgever. De keten loopt van de server via `Rijksdienst Issuing CA2` naar een zelfondertekende `Rijksdienst Root CA`. Die root zit niet in de certificatenlijst van je machine, en daarom weigert alles wat netjes valideert de verbinding. Met `curl -k` omzeil je dat, maar dat werkt alleen bij curl: Node en Python kennen zo'n vlag niet, dus Claude Code en scripts lopen er alsnog op vast.

Je hoeft dit maar één keer te doen. De server stuurt de root zelf mee, dus je kunt hem eruit halen:

```
openssl s_client -connect vlam-api.rijksweb.nl:443 -servername vlam-api.rijksweb.nl -showcerts </dev/null 2>/dev/null | awk '/BEGIN CERT/{n++} n==3' > ~/.config/rijksdienst-root-ca.pem
```

Controleer daarna de vingerafdruk, want een root die je uit de verbinding zelf haalt is per definitie de root van wie er antwoordde. Vergelijk hem met een beheerde Rijkslaptop of laat hem bevestigen door SSC-ICT:

```
openssl x509 -in ~/.config/rijksdienst-root-ca.pem -noout -fingerprint -sha256
```

Verwacht: `1A:04:61:6F:8D:6A:2F:6E:90:3A:7B:56:E3:7F:75:BF:9F:76:B9:16:0C:D4:D2:27:71:7E:C9:FC:95:5F:33:6D`

Zet daarna deze regels in je `~/.zshrc`, dan geldt het voortaan vanzelf:

```
export NODE_EXTRA_CA_CERTS="$HOME/.config/rijksdienst-root-ca.pem"
export REQUESTS_CA_BUNDLE="$HOME/.config/rijksdienst-root-ca.pem"
export CURL_CA_BUNDLE="$HOME/.config/rijksdienst-root-ca.pem"
```

Controleren of het werkt, zonder `-k`:

```
curl -s -o /dev/null -w 'HTTP %{http_code}, verify=%{ssl_verify_result}\n' https://vlam-api.rijksweb.nl/
```

Daar hoort `HTTP 200, verify=0` uit te komen.

Installeer deze root **niet** in je systeemsleutelhanger. Dan kan die CA namelijk voor elk willekeurig domein een geldig certificaat uitgeven en vertrouwt je machine dat, ook voor je bank of je mail. Met de aanpak hierboven geldt het vertrouwen alleen voor de gereedschappen waar je het nodig hebt.

#### 5. De API zelf uitproberen

Hiervoor heb je een API-sleutel van VLAM nodig. Zet die eerst in je omgeving, dan hoef je hem niet in elk commando te herhalen. Deze regel komt niet in je shell-geschiedenis als je hem laat voorafgaan door een spatie:

```
 export VLAM_KEY="jouw-sleutel"
```

Welke modellen er zijn, dit werkt zonder sleutel:

```
curl https://vlam-api.rijksweb.nl/v1/models
```

Een echte aanroep. Dit is de vorm die werkt:

```
curl https://vlam-api.rijksweb.nl/v1/chat/completions -H "Authorization: Bearer $VLAM_KEY" -H "content-type: application/json" -d '{"model":"vlam-medium-vast","messages":[{"role":"user","content":"zeg hallo"}]}'
```

En de vorm die Claude Code gebruikt. Deze geeft op dit moment een `403`, want de route staat niet open voor onze sleutels:

```
curl https://vlam-api.rijksweb.nl/v1/messages -H "Authorization: Bearer $VLAM_KEY" -H "content-type: application/json" -d '{"model":"vlam-medium-vast","max_tokens":64,"messages":[{"role":"user","content":"zeg hallo"}]}'
```

Heb je sectie 4 nog niet gedaan, zet er dan `-k` bij, anders weigert curl vanwege het certificaat.

Twee dingen om te weten als er iets misgaat. Een `401` betekent dat je sleutel niet meekomt of niet klopt. Een `500` heeft in ons geval niet aan VLAM gelegen maar aan onze eigen proxy die naar een verouderd adres wees, dus meld die bij ons voordat je hem bij VLAM meldt.

#### Als het niet werkt

Laat het in Mattermost weten.


#### Terugkoppeling aan het VLAM-team

Chat completions werken. We hebben twee vragen, en beide gaan over gebruik dat verder reikt dan een enkele aanroep.

**1. Agent coding via `/v1/messages`**

We willen VLAM gebruiken voor agent coding, concreet met Claude Code. Dat gereedschap praat de Anthropic Messages API en roept dus `POST /v1/messages` aan, niet `/v1/chat/completions`. LiteLLM ondersteunt dat endpoint, maar bij ons komt er een 403 uit:

```
{"error":{"message":"Access forbidden: Route /v1/messages not allowed","type":"auth_error","code":"403"}}
```

Dat lijkt op `allowed_routes` bij onze sleutel of ons team. Kan die route voor ons opengezet worden? En zo niet, hoe zien jullie agent coding op VLAM dan voor je? Wij kunnen er desnoods zelf een vertaallaag voor draaien, maar liever niet als het bij jullie een instelling is.

**2. Het certificaat**

De keten van `vlam-api.rijksweb.nl` eindigt in een zelfondertekende `Rijksdienst Root CA` van SSC-ICT, die niet in publieke certificatenlijsten zit. Onbeheerde machines moeten nu `-k` gebruiken, en gereedschap zoals Node kent zoiets niet eens, dus dat loopt gewoon vast.

De server stuurt die root zelf mee, dus we kunnen hem er technisch uit halen. Alleen vertrouwen we dan feitelijk wie er antwoordde, en dat is geen verificatie. Kunnen jullie deze SHA-256-vingerafdruk van de root bevestigen, dan kunnen wij hem gericht vertrouwen:

```
1A:04:61:6F:8D:6A:2F:6E:90:3A:7B:56:E3:7F:75:BF:9F:76:B9:16:0C:D4:D2:27:71:7E:C9:FC:95:5F:33:6D
```

**Ter informatie, en dit was onze eigen fout**

We hebben eerder gezien dat `vlam-medium-vast` een 500 gaf en `vlam-medium-prepaid` een 401. Dat lag aan ons: onze proxy had de hostnaam eenmalig opgezocht en bleef een oud IP-adres gebruiken nadat jullie van adres waren gewisseld. Dat is opgelost en verder geen actie voor jullie. We noemen het voor het geval het bij jullie in een logboek is opgedoken.
3. Het certificaat komt uit jullie eigen PKI en de keten eindigt in een zelfondertekende `Rijksdienst Root CA`. Die wordt door de server meegestuurd, dus we kunnen hem er zelf uit halen, maar dan vertrouwen we in feite gewoon wie er antwoordde. Kunnen jullie deze SHA-256-vingerafdruk van de root bevestigen, dan kunnen wij hem gericht vertrouwen: `1A:04:61:6F:8D:6A:2F:6E:90:3A:7B:56:E3:7F:75:BF:9F:76:B9:16:0C:D4:D2:27:71:7E:C9:FC:95:5F:33:6D`
