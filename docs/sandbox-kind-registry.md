# De registry naast het sandbox-cluster

De sandbox heeft een image-registry die **naast** het kind-cluster draait, niet erin: een
losse docker-container `kind-registry` op `127.0.0.1:5001`. Er is geen manifest van, dus dit
staat nergens anders. `scripts/setup-kind-registry.sh` zet hem neer, aangeroepen door
`task sandbox:setup` direct na `sandbox:create-cluster`.

## Waarom naast het cluster

Het punt van de hele oefening: de container **overleeft een clusterherbouw**. Na
`task sandbox:destroy` of een reset staan alle lagen er nog, dus de eerste deploy erna
verstuurt alleen wat er veranderd is in plaats van het volledige image van bijna 1 GB.

De in-cluster `rig-registry` kan dat niet: die bewaart zijn lagen op een PVC in het cluster,
dus een reset wist ze. Hij blijft waar hij voor is, images van projecten; zie
`docs/sandbox-image-deploy-via-registry.md`.

## Wat het script doet

1. Start de container `kind-registry` (`registry:2`) op `127.0.0.1:5001`, met
   `--restart=always` en `REGISTRY_STORAGE_DELETE_ENABLED=true`, als hij nog niet draait.
   Poort 5001 en niet 5000: op de gedeelde dev-server bindt `claude-dashboard` al op
   `127.0.0.1:5000`. Staat er een container zonder die variabele (neergezet voor het
   opruimen bestond), dan maakt het script hem opnieuw aan met `--volumes-from` de oude,
   zodat de lagen blijven. Lukt dat niet, dan zet het de oude terug.
2. Schrijft op elke node `/etc/containerd/certs.d/localhost:5001/hosts.toml` die naar
   `http://kind-registry:5000` wijst.
3. Hangt de registry aan het docker-netwerk `kind`, anders kan de node hem niet bereiken.
4. Zet de configmap `local-registry-hosting` in `kube-public` (KEP-1755), waar gereedschap
   als skaffold en tilt de lokale registry in opzoekt.

Het script is idempotent: nog een keer draaien verandert niets aan een cluster dat al klaar is.

## De voorwaarde in de kind-config

`sandboxed-local/kind-config.yaml` bevat:

```yaml
containerdConfigPatches:
- |-
  [plugins."io.containerd.grpc.v1.cri".registry]
    config_path = "/etc/containerd/certs.d"
```

Zonder die regel leest containerd de `hosts.toml` niet en pullt de node niets, zonder dat
er tot de eerste deploy iets misgaat. Het script controleert daarom per node of containerd
een `config_path` kent, en weigert als dat niet zo is.

Een kind-config geldt alleen bij `kind create cluster`, dus een bestaand cluster dat de
regel mist moet opnieuw gebouwd worden.

## Opruimen

Pusht de deploy naar deze registry, dan zet elke deploy een nieuwe tag neer.
`task sandbox:prune-registry` (script `scripts/prune-kind-registry.sh`) houdt dat klein:

1. Per repository gaan de tags weg waarvan de image ouder is dan `RETENTIE_DAGEN`
   (standaard 2). Het moment komt uit het `created`-veld van de image-config; de registry
   houdt zelf geen tagdatum bij. De nieuwste image per repository blijft altijd staan, ook
   bij `RETENTIE_DAGEN=0`.
2. Een index van buildx (image plus attestation) gaat met zijn kinderen weg, anders
   houden die hun lagen vast.
3. Daarna draait `registry garbage-collect` in de container. Die verwijdert alleen blobs
   waar geen manifest meer naar verwijst.

Omdat elke build dezelfde base-, tooling- en dependencylagen deelt, blijven die aan de
nieuwste tag hangen. Het opruimen wist dus alleen de applagen van oude builds. Krimpt de
registry fors, dan klopt er iets niet en is de volgende deploy weer een volle push. Het
script meldt de omvang voor en na.

De buildcache van de buildx-builder (`sandbox:build-builder`) leeft in het volume van die
builder, niet in de registry, en het script spreekt alleen de registry-container aan.

```bash
task sandbox:prune-registry                    # standaard: 2 dagen
task sandbox:prune-registry RETENTIE_DAGEN=0   # alles behalve de nieuwste
```

### Waarom in het deploy-pad en niet in een cron

`sandbox:update-operations-manager` roept het opruimen aan als laatste stap, na de
uitrol. Dat is de enige plek:

- Een garbage collect naast een lopende push kan lagen weggooien die net geupload zijn en
  nog aan geen manifest hangen (de garbage-collect-doc van distribution waarschuwt
  hiervoor). In het deploy-pad is de eigen push klaar; een cron op de server weet dat niet.
  De taak neemt zelf geen lock, dus twee gelijktijdige deploys op dezelfde registry blijven
  een risico.
- Wie pusht, ruimt op: de registry groeit alleen door deploys, dus zonder deploys hoeft er
  ook niets weg.

Draait er geen registry, of is het een registry van voor deze stap (zonder deletes), dan
slaat het script het opruimen over met een melding en exit 0: de deploy gebruikt de
registry nog niet en mag daar niet op falen. `task sandbox:setup-registry` zet het recht.

## Gebruik

```bash
docker tag mijn-image:dev localhost:5001/mijn-image:dev
docker push localhost:5001/mijn-image:dev
kubectl run proef --image=localhost:5001/mijn-image:dev --restart=Never
```

Geen `kind load` en geen pull-secret. De node haalt het image zelf op en pullt alleen de
lagen die hij mist.

## De operations-manager

`task sandbox:update-operations-manager` loopt zo:

1. `sandbox:setup-registry`, zodat een cluster zonder de containerd-patch hier al faalt en
   niet pas bij het pullen.
2. `sandbox:build-operations-manager-image` pusht `localhost:5001/operations-manager:<tag>`.
   De tag is de korte commit. Staat er een ongecommitte wijziging of een nieuw, ongetrackt
   bestand in `operations-manager/`, dan komt er `-dirty-<hash van die wijzigingen>` achter:
   met dezelfde tag zou er niets uitrollen.
3. `sandbox:configure-operations-manager-image` zet de overlay in de werkboom op die tag.
   Dat gebeurt bij elke deploy, zodat een overlay uit een eerdere checkout niet stil een
   ander image uitrolt.
4. Apply en `rollout status`. Een `rollout restart` is niet meer nodig: de tag verandert,
   dus de podspec ook.

De algemene `task update-operations-manager` laadt met `kind load`. Na `sandbox:setup` leest
hij de sandbox-env, dus hij stopt dan meteen met een verwijzing naar deze taak.

`task sandbox:skaffold-dev` pusht naar dezelfde registry (`build.local.push`). De dev- en
debug-overlay hernoemen de ghcr-image naar `localhost:5001/operations-manager`, want
skaffold vervangt alleen verwijzingen met de naam van zijn artifact.

`sandbox-deploy` (de dclaude-helper, niet in deze repo) en de CMP-image
(`sandbox:build-cmp-image`) gebruiken nog `kind load`.
