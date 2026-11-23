# Een container die niet start, is niet gecrasht

## Wat het is

Een container die nooit is gestart en een applicatie die crasht, komen bij Kubernetes
allebei in `CrashLoopBackOff` terecht. Het portaal meldde daardoor in beide gevallen
"Applicatie crasht herhaaldelijk, bekijk de logs". Bij een container die niet start is dat
dubbel verkeerd: er is geen applicatie die crasht, en er zijn geen logs, want er heeft
nooit een proces gelopen. Je moest in ArgoCD kijken om te zien wat er echt stond.

Sinds RC-516 meldt het portaal "Container kan niet starten", met de oorspronkelijke
melding van Kubernetes eronder.

## De meting

Een component op een Fundament-cluster startte niet. Het startcommando was ingevuld als
een YAML-lijst:

```
sh, -c, "date > /data/index.html && exec /docker-entrypoint.sh nginx -g 'daemon off;'"
```

Het veld splitst op spaties, dus runc zocht een programma dat `sh,` heet:

```
Error: failed to create containerd task: failed to create shim task: OCI runtime create
failed: runc create failed: unable to start container process: error during container
init: exec: "sh,": executable file not found in $PATH
```

Dat bericht stond wel in een `Failed`-event in de namespace, maar haalde de kaart niet.
Vier dingen hielden het tegen:

| | wat er gebeurde |
|---|---|
| het event had geen vertaling | `interpret_events` gooide elk Warning-event zonder vertaling weg |
| de oorspronkelijke tekst ging niet mee | `interpret_argocd_errors` gaf alleen titel en suggestie door |
| `BackOff` kreeg de verkeerde titel | "Applicatie crasht herhaaldelijk", voor een container zonder logs |
| het event leek te oud | de leeftijd werd gemeten op het EERSTE voorkomen |

Die laatste is de gemeenste: een repeterend event houdt de `creationTimestamp` van zijn
eerste voorkomen. Dus: een pod die al lang faalt, verliest zijn events uit de lijst,
ook al traden ze op dat moment nog steeds op.

## Wat er veranderd is

In `opi/services/event_interpreter.py`:

- **Een onvertaald Warning-event komt door** als "Melding van Kubernetes: `<reason>`", met
  zijn eigen tekst, als INFORMATIONAL. Een reason uit `_NOISE_REASONS` en een event zonder
  tekst blijven weg. De kaart wordt hier niet druk van: er worden alleen Warnings
  opgehaald, `cm-acme-*` valt af, `gather_deployment_errors` houdt alleen over wat bij deze
  deployment hoort, en het pad wordt alleen gelopen als er al iets mis is.
- **Elk event draagt zijn oorspronkelijke tekst mee** als `original_message`, als die van
  de titel verschilt. De kaart toont dat in het uitklapblok.
- **Startfouten hebben een eigen vertaling**: `executable file not found`,
  `runc create failed`, `failed to create (containerd|shim) task`, `StartError`,
  `RunContainerError` en `CreateContainerError` worden "Container kan niet starten", met
  een suggestie over het startcommando.
- Dat patroon staat **na** `exec format error` en `permission denied`. Dat zijn ook
  runc-fouten, maar met een preciezere oorzaak, en die houden hun eigen melding.
- De titel is een **oorzaak**, geen symptoom: staat hij er voor een component, dan
  verdwijnt de crashmelding ernaast. Precies zoals een probe-kill dat al deed, zie
  [probe-kill-is-geen-crash.md](probe-kill-is-geen-crash.md).

In `opi/connectors/kubectl.py`:

- `_event_last_seen` leest het **laatste** voorkomen: `series.lastObservedTime`, dan
  `lastTimestamp`, dan `eventTime`, en anders `creationTimestamp`. Leeftijd, volgorde en de
  getoonde tijd gaan daar nu op. Events uit `events.k8s.io` laten `lastTimestamp` leeg en
  vullen `eventTime` en `series`, dus alle vier de velden zijn nodig.
- Het sorteren gebeurt in Python in plaats van met `--sort-by`, op dezelfde waarde.

## Hadden we dit bij het opslaan kunnen zien?

Ja, en dat gebeurt nu. `CommandLineValidator` in `opi/forms/editables/validators.py`
weigert een startcommando dat met `[` begint, of waarvan het eerste argument op een komma
eindigt, en toont de goede vorm:

```
Scheid de argumenten met spaties, niet met komma's of haken.
Bijvoorbeeld: sh -c "date > /data/index.html && exec nginx"
```

Een programmanaam eindigt nooit op een komma, dus dit raakt geen geldig commando:
`echo hello, world` blijft gewoon geldig. De regel geldt alleen voor een getypte string,
niet voor een lijst die al als lijst in het projectbestand staat.

## Wat hier nog niet in zit

- `get_application_pods` leest `state.waiting.message` en `lastState.terminated.message`
  niet. De oorzaak komt nu uit de events, niet uit de containerstatus.
- De limiet van 30 events per namespace wordt toegepast vóór het filter op deployment.
- De tekst verwijst bij een crash naar de logs van de vorige poging. Bij een container die
  nooit gestart is, bestaan die niet.

## Zie ook

- [probe-kill-is-geen-crash.md](probe-kill-is-geen-crash.md) voor hetzelfde mechanisme: een
  preciezere oorzaak verdringt de crashmelding.
- [deployment-state-and-health.md](deployment-state-and-health.md) voor hoe de
  deploymentkaart zijn oordeel opbouwt.
- [image-pull-backoff-detection.md](image-pull-backoff-detection.md) voor de andere fout
  die een pod nooit laat starten.
