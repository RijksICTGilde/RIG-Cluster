# The standard image already ships pgvector, so nothing needs compiling here. The plain
# "17" tag is CNPG's legacy image on Debian bullseye, whose security packages are gone
# from the Debian mirror; building pgvector on it fails at apt-get install.
FROM ghcr.io/cloudnative-pg/postgresql:17-standard-bookworm

USER root

# Copy full-text search dictionary files for Dutch and English
COPY tsearch_data/ /usr/share/postgresql/17/tsearch_data/

USER 26
