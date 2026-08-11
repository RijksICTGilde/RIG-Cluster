# Een uitrol op de dev-server

Hoe je een nieuwe Operations Manager op de sandbox van de gedeelde dev-server zet, zonder
de machine om te trekken. Er stond hier niets over opgeschreven, en daardoor verzon
iedereen zijn eigen weg; zo ontstond de storing van 11 augustus 2026 (load 34,8, nog 1 GB
van de 15 vrij, twee andere sessies aan het werk, en de build faalde alsnog).

## De machine

Eén Linux-server met 16 GB werkgeheugen en 4 kernen. Daarop draaien tegelijk:

- het kind-cluster `rig-sandbox` (de sandbox zelf, ~4-6 GB),
- een tweede kind-cluster voor integratietests,
- meerdere dclaude-sessies, elk met hun eigen werkkopie,
- Caddy, het dashboard, Forgejo, ntfy, Portainer.

Er is dus geen ruimte voor een build die pakt wat hij wil. De schijf is niet het probleem
(die zat op 10% tijdens de storing) - het geheugen is het.

## De weg

1. **Kijk of er ruimte is.** `uptime` en `free -g`, of laat de bouwtaak het doen: die
   weigert vanzelf (zie hieronder). Draait er een andere sessie te bouwen of te testen,
   wacht dan.
2. **Claim de sandbox.** Precies één PR staat er tegelijk op:
   ```bash
   orch sandbox status          # wie heeft hem, en tot wanneer
   orch sandbox claim <naam> --lease 60 --note "uitrol <commit>"
   ```
3. **Bouw en laad de image.**
   ```bash
   task sandbox:build-operations-manager-image
   ```
   Dit schrijft eerst `opi/version.json` uit git, controleert het vrije geheugen, zorgt
   voor een builder met een geheugengrens, bouwt met cache en laadt de image in Kind.
4. **Rol uit** (in een sessie zonder `kustomize` en de SOPS-sleutel gaat dit met de hand,
   anders `task sandbox:update-operations-manager`):
   ```bash
   kubectl -n rig-system set image deployment/operations-manager \
     operations-manager=operations-manager:latest
   kubectl -n rig-system set env deployment/operations-manager ZAD_VERSION="$(git rev-parse --short HEAD)"
   kubectl -n rig-system rollout status deployment/operations-manager --timeout=300s
   ```
   De rollout duurt makkelijk ~230 seconden; een timeout van 180s is te kort en zegt niets
   over de build.
5. **Controleer welke versie draait.** Niet overslaan - dit is de stap die "ik testte de
   oude image" tegenhoudt:
   ```bash
   curl -sk https://zad.sandbox.rijksapp.dev/version
   git rev-parse --short HEAD
   ```
6. **Geef de sandbox terug**: `sandbox-release` (of `orch sandbox release <naam>`).

In een dclaude-sessie doet `sandbox-deploy` stap 2 tot en met 5 in één keer; die bouwt met
de docker-standaardbuilder en dus zonder de geheugengrens hieronder. Gebruik hem als de
machine rustig is, en anders de taak uit stap 3.

## Wat de bouwtaak nu zelf bewaakt

**Een controle vooraf** (`scripts/build-preflight.sh`). Weigert te beginnen als er minder
dan 6 GB vrij is, en meldt dan wat er draait, zodat je weet wie je omver zou duwen. Die 6
GB is de grens van de build (4 GB) plus ruimte voor de docker-daemon en het laden in Kind.
Bewust toch doorgaan kan met `BUILD_PREFLIGHT_SKIP=1`, de grens verzetten met
`BUILD_MIN_AVAILABLE_MB`.

**Een grens op het geheugen** (`task sandbox:build-builder`). De standaardbuilder draait in
de docker-daemon en kent geen grens; een `docker-container`-builder wel. De grens staat op
**4 GiB**: ruim boven wat de zwaarste stap (`uv sync`) nodig heeft, en het laat ~11 GB over
voor het cluster en de andere sessies. Lager afstellen laat een build sneuvelen die het wel
had gekund. Verzetten kan met `SANDBOX_BUILD_MEMORY=6g`.

**Cache.** `operations-manager/Dockerfile` doet drie `apt-get`-rondes (basispakketten,
kubectl, skopeo). Zonder cache haalt elke build die opnieuw op - normaal traag, en fataal
als de pakketspiegels slecht bereikbaar zijn (`Ign:`-regels in de uitvoer betekenen precies
dat: apt kon een spiegel niet bereiken). De builder houdt zijn cache vast tussen builds
door, en met `SANDBOX_CACHE_IMAGE` kun je er een registry-cache naast zetten, dezelfde vorm
als `publish-operations-manager` al gebruikt.

### De builder wordt gedeeld, de buildx-store niet

De buildkit-container draait in de docker-daemon en die is van de machine: alle sessies
delen dus dezelfde builder en dezelfde buildcache. Wat NIET gedeeld is, is de buildx-store
waarin hij geregistreerd staat (`~/.docker/buildx`) - die is van de sessie. Een sessie die
de builder niet zelf aanmaakte kent hem daarom niet, en `docker buildx build --builder`
faalt daar met `no builder "rig-sandbox-builder" found` terwijl de container gewoon draait.

`task sandbox:build-builder` vangt dat op: hij kijkt naast `docker inspect` (de daemon) ook
naar `docker buildx inspect` (de store van deze sessie), en registreert de builder alsnog
met `docker buildx create --bootstrap`. Dat neemt een bestaande container met dezelfde naam
over, dus de buildcache blijft staan. Draai die taak (of gewoon de bouwtaak, die roept hem
aan) één keer in elke nieuwe sessie.

### De toets of de cache pakt

Bouw twee keer achter elkaar. De tweede build hoort merkbaar sneller te zijn en **geen
enkele apt-regel** te tonen. Blijft apt draaien, dan pakt de cache niet.

Doe die toets niet terwijl er andere sessies aan het werk zijn.

## Niet doen: images opruimen

De verleiding bij een volle machine is `docker image prune`. Dat lost niets op - de schijf
was het probleem niet - en het gooit juist de cache weg die de apt-rondes overslaat.

## Een dclaude-sessie is geen ontsnapping aan de cyclus

Een `orch`-taak doorloopt bouwen, review en security. Voor een uitrol is dat te veel: er
valt geen diff na te kijken. De uitwijk was een losse `dclaude --detach`-sessie, maar die
werkt niet: zo'n sessie meldt bij het starten `Task registered: <naam>`, en de orkestrator
zet daar een reviewronde op (hier leverde dat een reviewsessie op PR #57 op, die met de
hand gestopt moest worden). Het verschil dat de uitwijk moest maken bestaat dus niet.

En hij is niet zomaar weg te halen. De sessie killen helpt niet: de taak blijft staan en de
PR blijft open, dus de orkestrator zet er telkens een nieuwe review op. Dat gaat zo:

```bash
orch cancel <TASK-ID> -c "reden"        # kilt de sessies, sluit de PR
orch cancel <TASK-ID> --delete-branch   # en haalt de branch weg
```

(In het plan heet dit `orch stop`; het commando in de CLI is `orch cancel`.)

Een handeling **zonder** cyclus bestaat vandaag niet in `orch`: `orch add` zet een taak in
`queued`/`approved`/`review`, en `dispatch` stuurt hem de gewone weg op. Zolang dat er niet
is, is de eerlijke conclusie: **een uitrol doe je met de hand**, langs de stappen hierboven,
en niet door er een sessie op te zetten.

## Als het cluster stuk is en niet je build

Symptoom: alles in `rig-system` in CrashLoopBackOff, `could not translate host name
rig-db-rw`, 502 op `https://zad.sandbox.rijksapp.dev`. Kijk dan eerst naar `kube-proxy`:

```bash
kubectl -n kube-system logs -l k8s-app=kube-proxy --tail=20
```

Staat daar `failed complete: too many open files`, dan is de inotify-grens van de host op
(veel containers en twee kind-clusters op één machine). Verhogen:

```bash
sudo sysctl -w fs.inotify.max_user_instances=512
sudo sysctl -w fs.inotify.max_user_watches=524288
kubectl -n kube-system delete pod -l k8s-app=kube-proxy
```

Zet het in `/etc/sysctl.d/99-kind.conf` om het over een herstart heen te houden. Zonder
`kube-proxy` werkt de service-VIP niet, valt CoreDNS om en crasht alles wat een database of
Keycloak zoekt - dat lijkt op een mislukte uitrol maar staat er los van.

## Open besluit: moet er op deze machine wel gebouwd worden?

Het doel is een draaiende versie, niet een build. Elders bouwen (CI) en het cluster de image
laten trekken haalt dit probleem structureel weg. Dat is een grotere verbouwing en hoort een
eigen besluit te zijn; het staat hier genoteerd, niet gedaan.
