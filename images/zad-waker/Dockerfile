# Build the static waker binary...
FROM golang:1.27.3-alpine AS build
WORKDIR /src
COPY go.mod ./
COPY main.go ./
# CGO off + static so it runs on distroless static (no libc).
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /zad-waker .

# ...and ship it on distroless static as non-root.
FROM gcr.io/distroless/static-debian12:nonroot@sha256:afa5c872c891853ca7fcf1f12c3edb23f7eeef36189728842dd51042ff57f7ab
COPY --from=build /zad-waker /zad-waker
# Size the Go runtime to the container rather than the node.
ENV GOMAXPROCS=1 GOMEMLIMIT=56MiB
USER 1001
EXPOSE 8080
ENTRYPOINT ["/zad-waker"]
