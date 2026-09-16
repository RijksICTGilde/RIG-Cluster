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
   `--restart=always`, als hij nog niet draait. Poort 5001 en niet 5000: op de gedeelde
   dev-server bindt `claude-dashboard` al op `127.0.0.1:5000`.
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
   De tag is de korte commit. Staat er een ongecommitte wijziging in `operations-manager/`,
   dan komt er `-dirty-<hash van de diff>` achter: met dezelfde tag zou er niets uitrollen.
3. `sandbox:configure-operations-manager-image` zet de overlay in de werkboom op die tag.
   Dat gebeurt bij elke deploy, zodat een overlay uit een eerdere checkout niet stil een
   ander image uitrolt.
4. Apply en `rollout status`. Een `rollout restart` is niet meer nodig: de tag verandert,
   dus de podspec ook.

`task sandbox:skaffold-dev` pusht naar dezelfde registry (`build.local.push`). De dev- en
debug-overlay hernoemen de ghcr-image naar `localhost:5001/operations-manager`, want
skaffold vervangt alleen verwijzingen met de naam van zijn artifact.

`sandbox-deploy` (de dclaude-helper, niet in deze repo) en de CMP-image
(`sandbox:build-cmp-image`) gebruiken nog `kind load`.
