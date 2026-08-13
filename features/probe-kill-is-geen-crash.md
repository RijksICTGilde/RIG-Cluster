# Een falende health-check is geen crash

## Wat het is

Een pod die door een falende liveness-probe wordt gekild en een pod die echt crasht,
zien er in Kubernetes bijna identiek uit. Het portaal meldde daardoor in beide gevallen
"Applicatie crasht herhaaldelijk". Dat verschil is voor de gebruiker het hele verschil:
bij "crasht" ga je je applicatie debuggen, bij "de probe komt er niet doorheen" pas je
je health-instelling aan. Wie de eerste melding gelooft, zoekt in de verkeerde code.

Sinds RC-105 melden de twee gevallen verschillend, en de probe-melding noemt de poort
waarop de probe strandde.

## De meting

Waargenomen op `/projects/ma-axk/deployments/productie`. Dat project stond niet meer op
de sandbox toen dit werd uitgezocht (het cluster was herbouwd), dus is het mechanisme
nagespeeld met twee pods naast elkaar in namespace `rc105-meting`: `probefail` draait
prima maar heeft een liveness-probe op een dichte poort, `echtcrash` stopt met `exit 1`.

| | `probefail` (probe faalt) | `echtcrash` (crasht echt) |
|---|---|---|
| pod-status na 4 herstarts | `CrashLoopBackOff` | `CrashLoopBackOff` |
| `lastState.terminated.reason` | `Error` | `Error` |
| `lastState.terminated.exitCode` | 137 | 1 |
| `BackOff`-event | `Back-off restarting failed container app in pod ...` | `Back-off restarting failed container app in pod ...` |
| `Unhealthy`-event | `Liveness probe failed: dial tcp 10.244.0.89:9999: connect: connection refused` | - |
| `Killing`-event | `Container app failed liveness probe, will be restarted` | - |

Drie dingen om te onthouden:

1. **Een probe-kill komt óók in `CrashLoopBackOff` terecht.** Blijft de probe falen, dan
   gaat de kubelet backoffen en schrijft hij hetzelfde `BackOff`-bericht als bij een
   echte crash. Dit is de bron van de verkeerde melding.
2. **`lastState.terminated.reason` draagt het onderscheid niet.** Hij is in beide
   gevallen `Error`. De exitcode verschilt hier (137 tegen 1), maar 137 is enkel
   "gekild met SIGKILL" en dat overkomt een echt crashende container ook.
3. **Het `Unhealthy`-event draagt het onderscheid wel.** De kubelet schrijft "Liveness
   probe failed" alleen als hij een *draaiende* container aan het killen is. Een
   container die uit zichzelf stopt haalt dat event niet.

## Wat er veranderd is

In `opi/services/event_interpreter.py`:

- Een `Unhealthy`-event met **liveness** of **startup** in het bericht wordt nu vertaald
  naar "Health-check faalt, de container wordt herstart", met de poort uit het bericht in
  de toelichting. Daarvoor viel het onder de algemene vertaling "Health-check gefaald".
- Die melding is een **oorzaak**, geen symptoom. "Health-check gefaald" stond in
  `_SYMPTOM_TITLES` en werd door `_suppress_symptoms` weggegooid zodra er een
  crashmelding naast stond -- precies de melding die de waarheid vertelde verdween dus.
- Staat er voor hetzelfde component een probe-kill, dan verdwijnt nu andersom de
  crashmelding: die is onwaar. Beide komen van dezelfde kubelet-backoff, maar alleen de
  probe-kill weet waarom de container omging.

Het onderscheid werkt beide kanten op:

- Een component dat echt crasht heeft geen `Unhealthy`-liveness-event, dus blijft
  "Applicatie crasht herhaaldelijk" gewoon staan.
- Een **readiness**-probe kilt de container niet. Bij een crashende app is een falende
  readiness-probe het *gevolg*, en hij blijft daarom een symptoom dat onder de
  crashmelding verdwijnt.

## Hadden we dit bij het opslaan kunnen zien?

**Ja, en het is klein werk.** De health-check-poort wordt nergens getoetst tegen de
poorten die het component aanbiedt: `HEALTH_CHECK_PORT_EDITABLE` gebruikt een
`RangeValidator(1024, 65535)` en verder niets. Een probe op een poort waar het component
niet luistert is dus gewoon op te slaan, en valt pas een half uur later op als een
"crashende" pod.

Waar het hoort: `ComponentServicesEnforcer` in `opi/forms/editables/enforcers.py`. Die
loopt de componenten mét index langs en doet al een kruiscontrole van exact deze vorm
(`_validate_memory_request_limit`: request mag de limit niet overschrijden, met een
`FieldError` op het juiste indexpad). Zowel `ports.inbound` als
`services{health-check}/config/port` staan daar in hetzelfde component-dict.

Waarom het **niet** in RC-105 zit: het is een gedragswijziging op de opslagweg. Een
bestaand projectbestand met een probe-poort buiten `ports.inbound` zou ineens niet meer
op te slaan zijn, en dat verdient een eigen PR met een eigen afweging (weigeren of
waarschuwen, en wat te doen met wat er nu al staat).

## Zie ook

- `features/health-check-service.md` -- de dienst zelf en zijn configuratie.
- `features/deployment-state-and-health.md` -- hoe de deploymentkaart zijn oordeel opbouwt.
