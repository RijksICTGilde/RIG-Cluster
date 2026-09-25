# Onze spiegel van de MinIO-client: een scratch-image met alleen /usr/bin/mc erin.
#
# Waarom een spiegel. MinIO distribueert mc niet meer op een adres dat een build kan
# gebruiken: docker.io/minio en quay.io/minio geven 401 op elke tag, dl.min.io geeft 410
# Gone. Wat overblijft is de GitHub-release, en die kan hetzelfde overkomen: de productiepin
# van 2 september haalt mc nog van dl.min.io en is daardoor vandaag niet meer te herbouwen.
# Die afhankelijkheid hoort niet in het bouwpad van OPI te zitten, dus wordt de release hier
# een keer opgehaald en staat mc daarna in ons eigen register.
#
# /usr/bin/mc is hetzelfde pad als de quay-image had, zodat een COPY --from bij de afnemer
# ongewijzigd blijft werken.
#
# De checksum staat hier LITERAAL en wordt niet naast de binary opgehaald. Die .sha256sum
# komt van dezelfde bron als de binary zelf, dus hij controleert alleen de overdracht: wie
# het artefact kan wijzigen, wijzigt het bestand ernaast mee. Een literaal in dit bestand is
# de grendel: hij laat een gewijzigd artefact omvallen, en een nieuwe waarde is een regel in
# een diff die iemand goedkeurt. Bij een nieuwe MC_VERSION horen dus twee nieuwe sommen; de
# hele weg staat in README.md hiernaast.
ARG MC_VERSION=RELEASE.2025-08-13T08-35-41Z

# Op BUILDPLATFORM, want deze stage haalt alleen een binary op voor TARGETARCH. Zonder dat
# draait hij onder emulatie en heeft een multi-arch build qemu nodig.
FROM --platform=$BUILDPLATFORM alpine:3.22 AS haal

ARG MC_VERSION
ARG TARGETARCH
ARG MC_SHA256_AMD64=01f866e9c5f9b87c2b09116fa5d7c06695b106242d829a8bb32990c00312e891
ARG MC_SHA256_ARM64=14c8c9616cfce4636add161304353244e8de383b2e2752c0e9dad01d4c27c12c

RUN apk add --no-cache ca-certificates curl \
    && case "${TARGETARCH}" in \
         amd64) MC_SHA256="${MC_SHA256_AMD64}" ;; \
         arm64) MC_SHA256="${MC_SHA256_ARM64}" ;; \
         *) echo "geen mc-checksum voor TARGETARCH=${TARGETARCH}" >&2; exit 1 ;; \
       esac \
    && MC_ASSET="mc.linux-${TARGETARCH}.${MC_VERSION}" \
    && curl -fsSL -o /mc "https://github.com/minio/mc/releases/download/${MC_VERSION}/${MC_ASSET}" \
    && echo "${MC_SHA256}  /mc" | sha256sum -c -

FROM scratch
COPY --from=haal --chmod=755 /mc /usr/bin/mc
ENTRYPOINT ["/usr/bin/mc"]
