# De Keycloak van het platform: de basisversie uit quay, met onze providers er al in.
# Waarom een eigen image, en hoe bouwen en promoveren gaat: features/keycloak-eigen-image.md.
#
# Buildcontext is de repo-wortel, zoals images/cmp-kustomize-sops.

# BUILDPLATFORM, want jars zijn architectuuronafhankelijk: zonder dit draait deze hele fase
# per doelarchitectuur opnieuw, en de tweede keer onder emulatie.
FROM --platform=$BUILDPLATFORM maven:3.10.0-eclipse-temurin-21 AS providers

WORKDIR /bouw
COPY images/keycloak-zad/extensions/relay-email-sender relay-email-sender
COPY images/keycloak-zad/extensions/custom-mapper custom-mapper

# Elke module houdt zijn eigen pom en zijn eigen versie; `package` draait hun toetsen mee.
RUN mvn -B -f relay-email-sender/pom.xml clean package \
 && mvn -B -f custom-mapper/pom.xml clean package

# De assets van het thema. Ook op BUILDPLATFORM: het resultaat is CSS, fonts en een
# JavaScript-bundel, en dus architectuuronafhankelijk.
FROM --platform=$BUILDPLATFORM node:26.13.0-alpine AS thema-assets

WORKDIR /thema
COPY images/keycloak-zad/theme/package.json images/keycloak-zad/theme/package-lock.json ./

# `npm ci` en niet `npm install`: de lockfile pint @nldd/design-system op één versie en
# controleert elke tarball op zijn integriteitssom.
RUN npm ci --no-audit --no-fund

# Alleen wat de pagina's laden. De stijlbladen verwijzen relatief naar ../fonts, dus de
# twee mappen moeten naast elkaar blijven staan.
RUN mkdir -p /thema/nldd \
 && cp -r node_modules/@nldd/design-system/dist/css /thema/nldd/css \
 && cp -r node_modules/@nldd/design-system/dist/fonts /thema/nldd/fonts \
 && cp -r node_modules/@nldd/design-system/dist/translations /thema/nldd/translations \
 && cp node_modules/@nldd/design-system/dist/nldd.min.js /thema/nldd/nldd.min.js \
 && cp node_modules/@nldd/design-system/dist/favicon.svg /thema/nldd/favicon.svg

FROM quay.io/keycloak/keycloak:26.8.1

# Met een jokerteken en niet met de versie erin: de versie staat in de pom van de module en
# hoort niet ook hier te staan. Vindt het jokerteken niets, dan faalt de bouw.
COPY --from=providers /bouw/relay-email-sender/target/keycloak-relay-email-sender-*.jar /opt/keycloak/providers/
COPY --from=providers /bouw/custom-mapper/target/keycloak-saml-nameid-mapper-*.jar /opt/keycloak/providers/

# Het thema als MAP onder themes/ en niet als jar onder providers/: een map vraagt geen
# META-INF/keycloak-themes.json en geen jar-stap, en Keycloak leest hem bij de start even
# goed. Zie features/keycloak-nldd-thema.md.
COPY images/keycloak-zad/theme/nl-design-system /opt/keycloak/themes/nl-design-system
COPY --from=thema-assets /thema/nldd /opt/keycloak/themes/nl-design-system/common/resources/nldd

# Keycloak neemt providers pas op bij een augmentatie. De bouwopties, --optimized en spi-providers
# zijn verdeeld over Dockerfile en deployment: zie features/keycloak-eigen-image.md.
RUN /opt/keycloak/bin/kc.sh build \
      --db=postgres \
      --health-enabled=true \
      --metrics-enabled=true \
      --features=admin-api \
      --spi-email-sender-provider=zad-relay

# De base zet deze uid zelf al; expliciet hier omdat de config-scan alleen de Dockerfile
# leest en een image zonder USER-regel als root-image beoordeelt (DS-0002).
USER 1000
