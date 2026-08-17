# De wekker die blijft vragen: 28.800 verzoeken per dag voor een deployment waar niemand naar kijkt

Gemeten op 17 augustus 2026, naar aanleiding van een waarneming op de sandbox: er komt een gestage stroom `GET /api/sleep-mode/{p}/{d}/status` binnen terwijl er niemand een wekkerpagina open heeft.

Dat laatste klopt, en het is niet de verklaring die voor de hand ligt. **De wekkerpagina is niet de poller.**

## Wat er gemeten is

Er zijn twee lussen, en ze gaan naar verschillende plekken.

- **De browsertab** pollt elke 2 seconden `/__zad/status` (`images/zad-waker/main.go:380`). Dat is een endpoint op de wekkerpod zelf en komt nooit bij OPI.
- **De wekkerpod** pollt elke 3 seconden `GET /api/sleep-mode/{p}/{d}/status` op OPI (`main.go:136-168`, interval uit `ZAD_POLL_INTERVAL_SEC: "3"` in `opi/services/catalog/sleep_mode/manifests.py:185`). Die lus draait, in de woorden van zijn eigen commentaar, "for the pod's lifetime" — onvoorwaardelijk, of er nu iemand kijkt of niet.

Zolang een deployment slaapt draait die pod, dus pollt hij. Niet één open tab is genoeg, maar **één slapende deployment met nul bezoekers**: 1200 verzoeken per uur, 28.800 per dag, per slapende deployment, tot iemand hem wekt.

Dat is de omgekeerde wereld: slaapstand bestaat om rust en resources te kopen.

De prijs per poll is bovendien geen kale cachehit. `flow.status` doet `KubectlConnector().get_deployment_status(...)`, en dat is `kubectl get deployments -n <ns> -o json` (`opi/connectors/kubectl.py:1059`). Dus elke 3 seconden een apiserver-aanroep per slapende deployment. Dat raakt precies de apiserver-hikken die eerder als oorzaak van vastlopers zijn aangewezen; zie `project_apiserver_hiccup_hypothesis` in de geheugennotities.

Eén ding werkt wél goed en moet zo blijven: zodra de app terug is stopt de pod met vragen (`if w.appReady.Load() { continue }`, `main.go:145`) en verdwijnt hij uit de Service. Het is geen lek ná het wekken, het is de slaaptoestand zelf.

**Meet dit opnieuw voordat je bouwt.** Dit is één sessie lezen plus een waarneming; bevestig het interval en het volume op het cluster, en tel wat een dag werkelijk oplevert.

## De reparatie

**De pod pollt alleen als er iemand wacht.** De wekkerpod weet wanneer er een browsertab hangt (die pollt hém) en wanneer hij zelf een wek-verzoek heeft gestuurd. Buiten die twee gevallen is er niemand voor wie het antwoord uitmaakt.

Dat is de hele wijziging, en hij zit in `images/zad-waker/`.

### Wat er NIET bij hoort

Hier stond een tweede richting: een korte cache op het statusendpoint in OPI, zodat meerdere wekkers samen één kubectl kosten. Die stond er met het argument dat het waker-image los uit de registry wordt gepulld, dus dat er pods met een oudere versie blijven draaien die je niet meer bereikt.

**Dat argument klopt niet.** De slaapstand draait alleen in de sandbox; alles wat er staat is opnieuw uit te rollen. Er is geen venster met oude en nieuwe wekkers naast elkaar, en dus is er niets voor die cache om af te vangen. Bouw hem niet. Noem hem hooguit in de PR als iets voor later, met deze reden erbij.

## De valkuil die het ontwerp bepaalt

**Een deployment kan gewekt worden zonder dat er een browsertab is.** Via zadctl, via de API, via de portal. Gebeurt dat, dan moet de wekkerpod dat nog steeds ontdekken, want zijn `/__zad/ready` is bewust omgekeerd (`main.go`): hij geeft 200 zolang de app NIET terug is, zodat hij het verkeer bedient, en 503 zodra hij eruit mag stappen. Stopt hij met pollen, dan leert hij nooit dat de app er weer is, blijft hij 200 geven, en blijft hij in de EndpointSlice staan **terwijl de app draait**. Dan serveert de wekker een wekpagina voor een applicatie die allang wakker is.

Dat is de enige manier waarop deze wijziging iets kan breken, en het is een ernstige. Het voorstel moet expliciet beantwoorden hoe de pod een wekactie van buitenaf opmerkt. Denkbare wegen, in oplopende omvang:

1. traag doorpollen in plaats van stoppen (bijvoorbeeld eens per 30 of 60 seconden zonder wachtenden, 3 seconden zodra er wel iemand is) — klein, en het houdt de eigenschap dat de pod het altijd zelf ontdekt;
2. OPI de pod laten seinen bij een wekactie — sneller, maar dat is een nieuwe weg van OPI naar een gebruikerspod en die moet dan ook beveiligd;
3. de wekactie de pod laten opruimen in plaats van hem het zelf te laten merken — het schoonst, maar het verlegt de verantwoordelijkheid en raakt de slaap/wek-flow.

Optie 1 lijkt me de beste ruil, maar dat is een voorkeur en geen besluit: kies op grond van wat er gebeurt als de seinweg faalt, niet op grond van hoe elegant hij is. Zet het antwoord in de PR.

## Wat er verder uit moet komen

**Het contract blijft staan.** `state` op `/status` levert `starting | ready`, en dat is precies waar het waker-image op vergelijkt (`main.go:162`). Niet aanraken; dit is de reparatie van een pollfrequentie, geen contractwijziging. RC-119 heeft er een tweede veld naast gezet met de echte slaaptoestand; dat is de plek voor iets nieuws.

**Een eigen uitrol.** Een wijziging in `images/zad-waker/` is een nieuw image in de registry en dus een aparte uitrolstap, los van OPI. Beschrijf die stap.

**De hoge frequentie heeft een reden.** 3 seconden is gekozen omdat iemand die op "wekken" drukt niet minutenlang naar een pagina wil kijken. Die eigenschap moet blijven op het moment dat het ertoe doet: wie wacht, wacht niet langer dan nu.

## Verifieerbaar

- Geteld op het cluster: het aantal `status`-verzoeken per uur voor een slapende deployment zonder bezoekers, vóór en na. Meld beide getallen.
- Gemeten dat wie wél wacht niet langer wacht: van klik tot herladen, vóór en na.
- Een test die aantoont dat een deployment die van BUITENAF gewekt wordt (API of CLI, geen browser) de wekker alsnog uit de Service laat stappen. Dit is de test die de valkuil hierboven afvangt; hij moet omvallen als je de trage doorpoll weghaalt.
- `uv run pytest tests/ -q` groen, plus `ruff check`, `ruff format`, `pyright`. Voor het Go-deel: `go vet` en `go build`.

## Wat er buiten valt

- De slaap- en wekflow zelf; die werkt.
- Het `state`-contract van het endpoint.
- Productie-uitrol.
