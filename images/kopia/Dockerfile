# Simple Kopia backup image for RIG
FROM alpine:3.19

ARG KOPIA_VERSION=0.22.3

# Install basics
RUN apk add --no-cache ca-certificates bash

# Install Kopia directly (hard-coded to x64 since that's what's used in production)
# Checksum staat literaal; zie images/README.md.
ARG KOPIA_SHA256_X64=d9e906e0813fc73addca7caa91c6ee33641e49e4fabe3462cd221445cc790c2d
RUN wget -q "https://github.com/kopia/kopia/releases/download/v${KOPIA_VERSION}/kopia-${KOPIA_VERSION}-linux-x64.tar.gz" -O /tmp/kopia.tar.gz \
    && echo "${KOPIA_SHA256_X64}  /tmp/kopia.tar.gz" | sha256sum -c - \
    && tar xzf /tmp/kopia.tar.gz -C /tmp \
    && mv /tmp/kopia-${KOPIA_VERSION}-linux-x64/kopia /usr/local/bin/kopia \
    && chmod +x /usr/local/bin/kopia \
    && rm -rf /tmp/kopia-${KOPIA_VERSION}-linux-x64 /tmp/kopia.tar.gz

# Verify it works
RUN kopia --version

# Set working dir (backup pods mount to /data)
WORKDIR /data

# Use sh as entrypoint to match existing manifest expectations
ENTRYPOINT ["/bin/sh"]
