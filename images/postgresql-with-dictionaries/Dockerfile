# The standard image already ships pgvector, so nothing needs compiling here. The plain
# "17" tag is CNPG's legacy image on Debian bullseye, whose security packages are gone
# from the Debian mirror; building pgvector on it fails at apt-get install.
# De build-datumstempel in de tag is het derde cijfer dat CNPG niet publiceert; hij wijst
# altijd naar dezelfde build, en de packageRule voor dit image in renovate.json houdt hem bij.
FROM ghcr.io/cloudnative-pg/postgresql:17.11-202610260819-standard-bookworm@sha256:fef8075b97b11a1fc12b387b9576809dbcda26a8fc68cb9416581f16856920f3

USER root

# Copy full-text search dictionary files for Dutch and English
COPY tsearch_data/ /usr/share/postgresql/17/tsearch_data/

USER 26
