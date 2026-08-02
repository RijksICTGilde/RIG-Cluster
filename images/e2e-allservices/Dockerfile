# Minimal all-services E2E test workload.
#
# Build stage compiles a statically-linked binary (no libc); the final stage is
# distroless-static (just the binary + CA certs for HTTPS OIDC/TLS S3). The
# result is a single tiny layer that runs as an arbitrary non-root UID and holds
# no writable state, satisfying the platform's forced securityContext.
#
# Build context is this directory:
#   docker buildx build --platform linux/amd64,linux/arm64 \
#     -t ghcr.io/minbzk/base-images/e2e-allservices:latest --push images/e2e-allservices
#
# probe_spec.json must be current before building:
#   uv run python scripts/generate_probe_spec.py   (guarded by test_probe_spec_drift.py)

FROM golang:1.23-alpine AS build
WORKDIR /src

# Cache module downloads separately from the source.
COPY go.mod go.sum ./
RUN go mod download

COPY . .
# CGO off + trimmed/stripped for the smallest static binary. TARGETARCH is set by
# buildx for multi-arch builds; default to amd64 for a plain `docker build`.
ARG TARGETARCH=amd64
RUN CGO_ENABLED=0 GOOS=linux GOARCH=${TARGETARCH} \
    go build -trimpath -ldflags="-s -w" -o /out/e2e-allservices .

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /out/e2e-allservices /e2e-allservices
# tcpSocket readiness/liveness probe the platform generates targets 8080.
EXPOSE 8080
ENTRYPOINT ["/e2e-allservices"]
