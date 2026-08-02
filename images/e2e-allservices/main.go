// Command e2e-allservices is a minimal test workload for the RIG-Cluster / ZAD
// platform. On startup it round-trips every platform service it is bound to
// (PostgreSQL incl. every extra schema + the read-only role, Redis, MinIO/S3,
// Keycloak/OIDC, and mounted PVCs), logs each step to stdout, and reports the
// outcome over HTTP (/, /healthz, /status).
//
// What it tests is driven by an embedded probe spec generated from OPI's own
// service registry (scripts/generate_probe_spec.py), so coverage tracks the
// platform automatically. See features/e2e-allservices-image.md.
package main

import (
	"log"
	"net"
	"net/http"
	"os"
	"time"
)

// listenAddr is fixed: the platform's generated readiness/liveness probe is a
// tcpSocket on 8080, so the server must bind :8080 on all interfaces.
const listenAddr = ":8080"

// refreshInterval re-runs the checks so /status reflects current reality, not
// just the boot snapshot.
const refreshInterval = 30 * time.Second

func main() {
	log.SetFlags(0) // we timestamp our own lines

	spec, err := loadSpec()
	if err != nil {
		// A broken embedded spec is a build error; still bind so the pod does not
		// crash-loop, and report the failure over HTTP/logs.
		logInfo("FATAL: %v", err)
		serveMinimal(err)
		return
	}
	logInfo("e2e-allservices starting: %d probe targets, listening on %s", len(spec.Targets), listenAddr)

	cache := newResultCache()
	mux := newMux(cache)

	// Bind FIRST so the tcpSocket:8080 probe passes immediately, even while the
	// first check round is still in flight.
	ln, err := net.Listen("tcp", listenAddr)
	if err != nil {
		log.Fatalf("cannot listen on %s: %v", listenAddr, err)
	}
	logInfo("listening on %s", listenAddr)

	// Run the checks in the background: first round now, then periodically.
	go func() {
		runAllChecks(spec, cache)
		ticker := time.NewTicker(refreshInterval)
		defer ticker.Stop()
		for range ticker.C {
			runAllChecks(spec, cache)
		}
	}()

	server := &http.Server{
		Handler:           mux,
		ReadHeaderTimeout: 5 * time.Second,
	}
	if err := server.Serve(ln); err != nil {
		log.Fatalf("http server stopped: %v", err)
	}
}

// serveMinimal keeps a bare :8080 listener alive that reports a fatal spec error,
// so a misbuilt image is diagnosable instead of an opaque crash-loop.
func serveMinimal(bootErr error) {
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte("ok\n"))
	})
	mux.HandleFunc("/", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
		_, _ = w.Write([]byte("e2e-allservices boot error: " + bootErr.Error() + "\n"))
	})
	server := &http.Server{Addr: listenAddr, Handler: mux, ReadHeaderTimeout: 5 * time.Second}
	if err := server.ListenAndServe(); err != nil {
		log.Fatalf("http server stopped: %v", err)
		os.Exit(1)
	}
}
