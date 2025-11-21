FROM ghcr.io/cloudnative-pg/postgresql:17

USER root

# Install pgvector extension
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        build-essential \
        postgresql-server-dev-17 \
        git \
        ca-certificates && \
    cd /tmp && \
    git clone --branch v0.7.4 https://github.com/pgvector/pgvector.git && \
    cd pgvector && \
    make && \
    make install && \
    cd / && \
    rm -rf /tmp/pgvector && \
    apt-get remove -y build-essential postgresql-server-dev-17 git && \
    apt-get autoremove -y && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Copy full-text search dictionary files for Dutch and English
COPY tsearch_data/ /usr/share/postgresql/17/tsearch_data/

USER 26
