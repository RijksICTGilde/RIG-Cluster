# The standard image already ships pgvector, so nothing needs compiling here. The plain
# "17" tag is CNPG's legacy image on Debian bullseye, whose security packages are gone
# from the Debian mirror; building pgvector on it fails at apt-get install.
# De build-datumstempel in de tag is het derde cijfer dat CNPG niet publiceert; hij wijst
# altijd naar dezelfde build, en de packageRule voor dit image in renovate.json houdt hem bij.
FROM ghcr.io/cloudnative-pg/postgresql:17.11-202611090810-standard-bookworm@sha256:68b520280153de84b294740b8a96d47918e31e4640cfa93cfb8131289aae3b88

USER root

# Copy full-text search dictionary files for Dutch and English
COPY tsearch_data/ /usr/share/postgresql/17/tsearch_data/

USER 26
