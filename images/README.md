We created the following images because there was no default solution:

mc: onze spiegel van de MinIO-client, een scratch-image met alleen /usr/bin/mc. MinIO distribueert
mc zelf niet meer op een adres dat een build kan gebruiken (401 op docker.io en quay.io, 410 op
dl.min.io), dus halen operations-manager/Dockerfile en de backup-image hem hier vandaan in plaats van
uit de GitHub-release. Zie images/mc/README.md. Build+publish: `task publish-mc`.

e2e-allservices: minimal, fast-booting test workload that round-trips every platform service it is
bound to (PostgreSQL incl. extra schemas + RO role, Redis, MinIO/S3, Keycloak/OIDC, PVCs) and reports
over HTTP (/, /healthz, /status). Used as the sandbox E2E all-services fixture; what it tests is
scan-driven from OPI's service registry. See images/e2e-allservices/README.md and
features/e2e-allservices-image.md. Build+publish: `task publish-e2e-allservices`.

keycloak-zad: de Keycloak van het platform met onze eigen providers er al in (de
relay-emailSender, de SAML-mapper met de vier SPI's, en het NL-design-thema van MinBZK). De twee
eigen jars worden uit de bron in deze repo gebouwd, het thema wordt tijdens de bouw opgehaald en
op checksum gecontroleerd, dus de pod haalt bij het starten niets meer op. Zie
features/keycloak-eigen-image.md. Build+push naar ghcr en de pin van de sandbox-overlay:
`task publish-keycloak-image`.

argocd-rig: upstream Argo CD v3.3.12 plus two namespace-sync fixes, so the cluster cache survives a
namespace being deleted or RBAC-restricted (we run namespace-mode with 47 namespaces). The patches in
images/argocd-rig/patches/ are the source of truth; `task src:verify` proves the build worktree is
exactly v3.3.12 + those patches. See images/argocd-rig/README.md. Build+publish: `task publish` in
that directory, to ghcr.io/minbzk/base-images/argocd-rig.

cmp-kustomize-sops: used as sidecar in ArgoCD, so we can apply kustomize with sops secrets, where the secret is
stored in the namespace, similar to how Flux would deploy.

To build and use locally:
docker build --no-cache --progress=plain -t rig-cmp-argo-kustomize-sops:latest .

TODO: push docker to external registry
docker push your-registry.com/rig-cmp-argo-kustomize-sops:latest

docker buildx build --platform linux/amd64,linux/arm64 -t ghcr.io/minbzk/base-images/rig-cmp-argo-kustomize-sops:latest --push .

LOCAL Kind, NOTE: use the correct clustername
kind load docker-image rig-cmp-argo-kustomize-sops:latest --name rig-sandbox

To run locally with shell access:
docker run --rm -it --entrypoint /bin/bash rig-cmp-argo-kustomize-sops:latest

CLUSTER_TYPE=sandboxed-local task bootstrap-argo-system KEY_FILE=security/sandbox-key.txt
kubectl rollout restart deployment argocd-repo-server -n rig-system

jupyterhub-hub en jupyterhub-user: de twee images van de JupyterHub-POC (RC-258). De hub is een
ZAD-component dat met KubeSpawner een pod per gebruiker start; de user-image is JupyterLab met VS
Code (code-server) als tegel. Alleen voor de sandbox, rechtstreeks naar de in-cluster registry. Zie
images/jupyterhub-hub/README.md, images/jupyterhub-user/README.md en docs/jupyterhub-poc-verslag.md.

## Een versie ophogen: nieuwe versie en nieuwe sommen in één diff

Elke `curl`- of `wget`-download van een binary of archief in een eigen Dockerfile heeft een
vaste versie en per architectuur een literale sha256 in de RUN-instructie. De checksum moet
letterlijk staan (niet opgehaald van een apart bestand), zodat een gewijzigd artefact de
build faalt en versiewijzigingen in een diff zichtbaar zijn. De volledige richtlijn voor
`images/mc/` staat in `images/mc/README.md`.

Ophogen gaat altijd in één diff: een nieuwe versie zonder nieuwe sommen faalt de build.

Meestal hoef je dat niet zelf te doen: Renovate stelt de nieuwe versie voor en draait
`scripts/renovate_checksums.py` in dezelfde branch, zodat de sommen in dezelfde PR staan.
Zie `features/renovate.md`. Hoog je zelf op, dan is datzelfde script de snelste weg, en
hieronder staat wat het doet, zodat je het ook met de hand kunt nalopen:

```bash
python3 scripts/renovate_checksums.py images/mc/Dockerfile
```

Het script toetst geen handtekening. Punt 1 hieronder blijft dus handwerk: bij een
Renovate-PR voor sops, kopia of helm-diff hoort de reviewer de handtekening bij die release
te toetsen en in de PR te noemen welke, net als wie zelf ophoogt.

1. Haal de sommen uit het checksumbestand dat het project zelf publiceert, bijvoorbeeld
   `checksums.txt` of `<artefact>.sha256sum` bij de release. Publiceert het project een
   handtekening (sops een sigstore-bundel, kopia een `checksums.txt.sig`, helm-diff een
   `.prov`), toets die dan en noem in de PR welke je hebt geverifieerd en hoe. Publiceert
   het project geen checksumbestand, bereken de som dan van het artefact zelf en zeg dat.
2. Zet de `ARG <TOOL>_VERSION` en de `ARG <TOOL>_SHA256_<ARCH>` in hetzelfde Dockerfile op
   de nieuwe waarden. Alleen de architecturen die de build echt maakt: zie `PLATFORMS` in
   de publicatietaak in `Taskfile.yaml`.
3. Bouw voor al die platforms en kijk of elke `sha256sum -c` er `OK` van maakt.

`operations-manager/python/tests/test_dockerfile_downloads_hebben_een_checksum.py` bewaakt
dat er geen download zonder checksum of zonder vaste versie bijkomt, en dat elke
`ARG <NAAM>_VERSION` een `# renovate:`-annotatie heeft. De sleutel van een gesigneerde
apt-repository staat daar in de uitzonderingslijst: apt toetst de pakketten daarna zelf
tegen die sleutel.
