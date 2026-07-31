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

#### Als het niet werkt

Laat het in Mattermost weten.


#### Terugkoppeling aan het VLAM-team

We kunnen de API bereiken, maar krijgen van geen enkel model een antwoord. Hieronder wat we precies hebben geprobeerd, zodat jullie het kunnen naslaan.

De sleutel werkt: op `/v1/chat/completions` worden we toegelaten. De modelnaam wordt ook herkend, want de foutmelding bevat `Received Model Group=vlam-medium-vast`. Het gaat daarna mis in de aanroep naar de dienst achter de proxy.

| Aanroep | Model | Resultaat |
|---|---|---|
| `POST /v1/chat/completions` | `vlam-medium-vast` | 500, `OpenAIException - Internal Server Error` |
| `POST /v1/chat/completions` | `vlam-medium-prepaid` | 401, `Authentication failed: You do not have permission to perform this action` |
| `POST /v1/completions` | `vlam-medium-vast` | 404, `Not Found` |
| `POST /v1/messages` | n.v.t. | 403, `Route /v1/messages not allowed` |
| `GET /v1/models` | n.v.t. | 200, geeft beide modelnamen terug |
| `GET /health/liveliness` | n.v.t. | 200 |

Het verzoek zelf is niet de oorzaak: met en zonder `max_tokens` levert hetzelfde resultaat op, en een ongeldig verzoek zou een 400 geven in plaats van een 500.

Dit gebruikten we, met een geldige sleutel:

```
curl https://vlam-api.rijksweb.nl/v1/chat/completions \
  -H "Authorization: Bearer $VLAM_KEY" \
  -H "content-type: application/json" \
  -d '{"model":"vlam-medium-vast","messages":[{"role":"user","content":"zeg hallo"}]}'
```

**Onze vragen**

1. `vlam-medium-vast` geeft een 500 en `vlam-medium-prepaid` een 401. Zijn die modellen op dit moment beschikbaar, en klopt het dat de 401 op een probleem aan jullie kant wijst en niet aan onze sleutel?
2. We willen dit gebruiken voor agent coding, concreet met Claude Code. Dat werkt via de Anthropic Messages API, dus `POST /v1/messages`. LiteLLM ondersteunt dat endpoint, maar het staat voor onze sleutel niet in `allowed_routes` en geeft daarom een 403. Kan die route opengezet worden? Zo niet, is agent coding dan op een andere manier voorzien?
3. Het certificaat komt uit jullie eigen PKI en de keten eindigt in een zelfondertekende `Rijksdienst Root CA`. Die wordt door de server meegestuurd, dus we kunnen hem er zelf uit halen, maar dan vertrouwen we in feite gewoon wie er antwoordde. Kunnen jullie deze SHA-256-vingerafdruk van de root bevestigen, dan kunnen wij hem gericht vertrouwen: `1A:04:61:6F:8D:6A:2F:6E:90:3A:7B:56:E3:7F:75:BF:9F:76:B9:16:0C:D4:D2:27:71:7E:C9:FC:95:5F:33:6D`
