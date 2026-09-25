# mc: onze spiegel van de MinIO-client

`ghcr.io/minbzk/base-images/mc:<MC_VERSION>` is een scratch-image met alleen `/usr/bin/mc`.
Afnemers zijn `operations-manager/Dockerfile` en `operations-manager/backup-image/Dockerfile`;
allebei halen ze mc met een `COPY --from` uit deze image. Waarom die spiegel er is en waarom
de checksum literaal in het Dockerfile staat: zie de kop van `Dockerfile` hiernaast.

## De spiegel vullen bij een nieuwe MC_VERSION

1. Haal de twee assets van de release op en neem hun sommen over:

   ```bash
   TAG=RELEASE.2025-08-13T08-35-41Z
   for arch in amd64 arm64; do
     curl -fsSLO "https://github.com/minio/mc/releases/download/${TAG}/mc.linux-${arch}.${TAG}"
     sha256sum "mc.linux-${arch}.${TAG}"
   done
   ```

2. Zet `MC_VERSION` en de twee `MC_SHA256_*` in `Dockerfile` op die waarden. De sommen zijn
   de grendel: bij de build valt een gewijzigd artefact om op `sha256sum -c`.
3. `task publish-mc`. Dat bouwt voor amd64 en arm64 en pusht naar GHCR, met
   `--provenance=false --sbom=false`. Die twee vlaggen zijn geen smaak: een image met de
   dubbele-manifestvorm die ze anders opleveren geeft 500 via het proxypad `ghcr-rig`
   (Quay PROJQUAY-10068). De tag volgt uit `ARG MC_VERSION` in het Dockerfile, dus die hoeft
   nergens anders gezet te worden.
4. Zet het package in GHCR op **public**, anders kan een build hem niet anoniem ophalen. Dat
   is de enige handmatige stap: het is een instelling van het package in GitHub en niet iets
   wat de build zet.
5. Toets wat je gepusht hebt. De manifest list moet linux/amd64 en linux/arm64 noemen en
   verder niets; een `unknown/unknown` erbij is de dubbele manifestvorm uit stap 3. Dat jij
   hem kunt ophalen zegt niets over stap 4, want je bent ingelogd, dus toets dat apart met
   een anoniem token:

   ```bash
   docker buildx imagetools inspect ghcr.io/minbzk/base-images/mc:${TAG}

   TOKEN=$(curl -s "https://ghcr.io/token?scope=repository:minbzk/base-images/mc:pull&service=ghcr.io" | jq -r .token)
   curl -sI -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer ${TOKEN}" \
     -H "Accept: application/vnd.docker.distribution.manifest.list.v2+json" \
     "https://ghcr.io/v2/minbzk/base-images/mc/manifests/${TAG}"
   ```

   Die laatste moet 200 geven. Een private package geeft er 401.
6. Zet daarna pas `ARG MC_VERSION` in de twee afnemers op dezelfde tag. Een afnemer die naar
   een tag wijst die hier nooit gevuld is, valt om op een manifest-404.
