# De registry naast het sandbox-cluster

De sandbox heeft een image-registry die **naast** het kind-cluster draait, niet erin: een
losse docker-container `kind-registry` op `127.0.0.1:5001`. Dat is niet af te leiden uit de
manifesten in deze repo, want er staat geen manifest van. `scripts/setup-kind-registry.sh`
zet hem neer, en `task sandbox:setup` roept dat script aan direct na `sandbox:create-cluster`.

## Waarom naast het cluster

Het punt van de hele oefening: de container **overleeft een clusterherbouw**. Na
`task sandbox:destroy` of een reset staan alle lagen er nog, dus de eerste deploy erna
verstuurt alleen wat er veranderd is in plaats van het volledige image van bijna 1 GB.

De in-cluster `rig-registry` kan dat niet. Die bewaart zijn lagen op een PVC in het cluster,
dus een reset wist ze. Daarnaast: htpasswd-auth (pull-secret in elke deploy), 256Mi geheugen,
en een kip-ei bij bootstrap, want OPI kan niets pullen voordat de registry draait. Die
registry blijft waar hij voor is, images van projecten; zie
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
er iets misgaat tot de eerste deploy. Het script controleert daarom per node of containerd
een `config_path` kent en weigert met een verwijzing naar de kind-config als dat niet zo is.

Een kind-config geldt alleen bij `kind create cluster`. Een bestaand cluster dat de regel
mist, moet opnieuw gebouwd worden; wijzigen achteraf werkt niet.

## Gebruik

```bash
docker tag mijn-image:dev localhost:5001/mijn-image:dev
docker push localhost:5001/mijn-image:dev
kubectl run proef --image=localhost:5001/mijn-image:dev --restart=Never
```

Geen `kind load` en geen pull-secret. De node haalt het image zelf op en pullt alleen de
lagen die hij mist.

Het omzetten van de buildtaken (`sandbox-deploy`, `task sandbox:update-operations-manager`)
naar deze weg is een aparte stap; die gebruiken nog `kind load`.
