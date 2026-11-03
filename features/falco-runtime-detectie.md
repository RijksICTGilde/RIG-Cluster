# Schot 2: runtime-detectie met Falco

Falco op elke node, met een eigen regelset die drie dingen meldt die in een draaiende
ZAD-workload niets te zoeken hebben. BIO2 8.16 (monitoring).

Waarom dit naast alle andere toetsen staat: de scanners en de admission-policies kijken naar
wat er binnenkomt. Dit kijkt naar wat er gebeurt. Als er ondanks alles iets binnen is, wil je
merken *dat* er water staat voordat het schip vol is.

## De drie regels

`infrastructure/bootstrap/infrastructure/falco/controller/base/regels.yaml`, naast de
upstream-regelset van Falco en niet in plaats daarvan. Alle drie zijn beperkt tot namespaces
die met `rig-` beginnen, dus tot de tenants en het platform zelf.

| Regel | Wat het betekent als hij vuurt |
|---|---|
| Shell in een ZAD-container | de pods draaien een applicatie, geen beheerterminal: handwerk buiten GitOps om, of iemand die binnen is |
| Service-account-token gelezen | een tenant-applicatie praat niet met de kube-api; wie dat token leest, zoekt rechten in het cluster |
| Pakketbeheerder of downloader gestart | de container haalt iets binnen dat niet in het image zat, en dus niet door de containerscan is gegaan |

De alerts gaan als JSON naar stdout, dus in dezelfde logstroom als de rest van het cluster.
Er komt geen tweede afleverpad bij. Falco zet daarnaast zijn eigen metrics op `:8765` in de
Prometheus-vorm, zodat het aantal alerts per regel te volgen is naast de bestaande scrapes
(`features/prometheus-monitoring.md`).

## De driver, en waarom dat uitmaakt

De DaemonSet draait `falcosecurity/falco` met `--modern-bpf`. Sinds Falco 0.40 is dat image
zelf zonder driver; `falco-no-driver` houdt op bij 0.39.2. Dat laadt geen
kernelmodule en vraagt geen privileged container: `hostPID` plus vier capabilities (`BPF`,
`PERFMON`, `SYS_RESOURCE`, `SYS_PTRACE`) is genoeg. De oudere drivers vragen wel een
privileged container, en dat is precies het schot dat we hier niet willen doorbreken om een
schot te plaatsen.

## Hoe je het opzet

```bash
task schotten:falco
kubectl -n rig-system logs daemonset/falco -f
```

Net als bij de admission-policies staat Falco nog niet in de GitOps-boom: dit is een
DaemonSet met node-toegang, en die zet je er bewust op, niet als bijwerking van een sync.

## Wat de toets bewijst, en wat niet

De job `falco-regels` in `.github/workflows/aanvalsproef.yml` haalt de regels uit de
ConfigMap en laat Falco ze valideren, samen met de upstream-regelset waar de macro's uit
komen (`spawned_process`, `container`, `open_read`, `shell_binaries`). Een schrijffout of een
macro die niet bestaat valt daar op.

De Falco-versie staat op een plek: de image-tag in de DaemonSet, die Renovate bijhoudt. De
job leest die tag en valideert daarmee, zodat de toets nooit tegen een andere Falco loopt dan
het cluster draait.

**Dat een regel ook werkelijk vuurt op een syscall is op een GitHub-runner niet te meten**:
dat vraagt eBPF op de host. Die meting is handwerk, op het sandbox-cluster:

```bash
task schotten:falco
kubectl -n rig-ron exec -it deploy/<een-pod> -- sh -c 'echo hallo'
kubectl -n rig-system logs daemonset/falco | grep "Shell gestart in een ZAD-container"
```

Verwacht: een JSON-regel met de gebruiker, het commando, de container, de namespace en de
pod. Wie dat heeft gedaan, zet de uitkomst op de PR of in een doorloopverslag, zodat de
volgende ronde niet opnieuw hoeft te raden of het werkt.
