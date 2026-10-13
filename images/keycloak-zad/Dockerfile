# De Keycloak van het platform: de basisversie uit quay, met onze providers er al in.
# Waarom een eigen image, en hoe bouwen en promoveren gaat: features/keycloak-eigen-image.md.
#
# Buildcontext is de repo-wortel, zoals images/cmp-kustomize-sops.

# BUILDPLATFORM, want jars zijn architectuuronafhankelijk: zonder dit draait deze hele fase
# per doelarchitectuur opnieuw, en de tweede keer onder emulatie.
FROM --platform=$BUILDPLATFORM maven:3.9-eclipse-temurin-21 AS providers

WORKDIR /bouw
COPY keycloak-migration/relay-email-sender relay-email-sender
COPY keycloak-migration/custom-mapper custom-mapper

# Elke module houdt zijn eigen pom en zijn eigen versie; `package` draait hun toetsen mee.
RUN mvn -B -f relay-email-sender/pom.xml clean package \
 && mvn -B -f custom-mapper/pom.xml clean package

# Versie en checksum staan hier uitgeschreven en niet als ARG: een ARG is met --build-arg te
# overschrijven, en dan bewaakt de controle niet meer wat er in git staat.
RUN curl -fsSL -o keycloak-nl-design-system.jar \
      "https://github.com/MinBZK/keycloak-theme/releases/download/v1.4.2/keycloak-nl-design-system.jar" \
 && echo "3adab8c9223127cf3aeb5b566fd9a08bab43bc79d9a0a92ff8a2ea1d2ecb9d35  keycloak-nl-design-system.jar" \
    | sha256sum -c -

FROM quay.io/keycloak/keycloak:25.0.6

# Met een jokerteken en niet met de versie erin: de versie staat in de pom van de module en
# hoort niet ook hier te staan. Vindt het jokerteken niets, dan faalt de bouw.
COPY --from=providers /bouw/relay-email-sender/target/keycloak-relay-email-sender-*.jar /opt/keycloak/providers/
COPY --from=providers /bouw/custom-mapper/target/keycloak-saml-nameid-mapper-*.jar /opt/keycloak/providers/
COPY --from=providers /bouw/keycloak-nl-design-system.jar /opt/keycloak/providers/

# Keycloak neemt providers pas op bij een augmentatie. De bouwopties, --optimized en spi-providers
# zijn verdeeld over Dockerfile en deployment: zie features/keycloak-eigen-image.md.
RUN /opt/keycloak/bin/kc.sh build \
      --db=postgres \
      --health-enabled=true \
      --metrics-enabled=true \
      --features=admin-api \
      --spi-email-sender-provider=zad-relay
