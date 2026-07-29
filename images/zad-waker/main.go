// Command zad-waker is the tiny pod that stands in for a sleeping ZAD deployment.
//
// When a deployment is in sleep-mode its app runs at replicas 0, so a separate,
// always-running pod must catch the request, show an "application is starting" page
// and ask ZAD to wake the deployment. This is that pod. It shares the app's Service
// and Ingress (same app label), so while the app has zero pods the waker is the only
// endpoint; once the app is back its readiness probe flips and it takes itself out of
// the EndpointSlice again.
//
// Design goals: minimal footprint (100m CPU / 64Mi), non-root, no writable paths,
// single-flight wake (a hundred simultaneous visitors cause exactly one wake call).
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"html/template"
	"log"
	"net/http"
	"os"
	"runtime/debug"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

// config is the waker's runtime configuration, all from the ConfigMap/Secret env.
type config struct {
	apiURL       string
	project      string
	deployment   string
	title        string
	description  string
	mode         string // auto | confirm | manual
	pollInterval time.Duration
	token        string
}

func loadConfig() config {
	poll := 3
	if v, err := strconv.Atoi(os.Getenv("ZAD_POLL_INTERVAL_SEC")); err == nil && v > 0 {
		poll = v
	}
	mode := strings.ToLower(strings.TrimSpace(os.Getenv("ZAD_WAKE_MODE")))
	switch mode {
	case "auto", "confirm", "manual":
	default:
		mode = "auto"
	}
	return config{
		apiURL:       strings.TrimRight(os.Getenv("ZAD_API_URL"), "/"),
		project:      os.Getenv("ZAD_PROJECT"),
		deployment:   os.Getenv("ZAD_DEPLOYMENT"),
		title:        firstNonEmpty(os.Getenv("ZAD_APP_TITLE"), os.Getenv("ZAD_DEPLOYMENT")),
		description:  os.Getenv("ZAD_APP_DESCRIPTION"),
		mode:         mode,
		pollInterval: time.Duration(poll) * time.Second,
		token:        os.Getenv("ZAD_WAKE_TOKEN"),
	}
}

func firstNonEmpty(values ...string) string {
	for _, v := range values {
		if v != "" {
			return v
		}
	}
	return ""
}

// waker holds the mutable state shared across handlers.
type waker struct {
	cfg      config
	client   *http.Client
	once     sync.Once   // single-flight: one wake call per pod lifetime
	appReady atomic.Bool // true once the app is back; flips /__zad/ready to 503
	state    atomic.Value // "idle" | "waking" | "ready" | "error"
	started  time.Time
}

func newWaker(cfg config) *waker {
	w := &waker{cfg: cfg, client: &http.Client{Timeout: 10 * time.Second}, started: time.Now()}
	w.state.Store("idle")
	return w
}

func (w *waker) currentState() string {
	if s, ok := w.state.Load().(string); ok {
		return s
	}
	return "idle"
}

// wake asks ZAD to wake the deployment, exactly once, with a small retry/backoff.
// In manual mode it never runs (an admin wakes via the UI/API); the poller still
// detects that and takes the waker out of traffic.
func (w *waker) wake() {
	if w.cfg.mode == "manual" {
		return
	}
	w.once.Do(func() {
		w.state.Store("waking")
		url := fmt.Sprintf("%s/api/sleep-mode/%s/%s/wake", w.cfg.apiURL, w.cfg.project, w.cfg.deployment)
		var lastErr error
		for attempt := 1; attempt <= 3; attempt++ {
			req, err := http.NewRequest(http.MethodPost, url, nil)
			if err != nil {
				lastErr = err
				break
			}
			req.Header.Set("X-Wake-Token", w.cfg.token)
			resp, err := w.client.Do(req)
			if err == nil {
				resp.Body.Close()
				if resp.StatusCode < 300 {
					log.Printf("wake requested for %s/%s (status %d)", w.cfg.project, w.cfg.deployment, resp.StatusCode)
					return
				}
				lastErr = fmt.Errorf("wake returned status %d", resp.StatusCode)
			} else {
				lastErr = err
			}
			time.Sleep(time.Duration(attempt) * 2 * time.Second)
		}
		log.Printf("wake failed for %s/%s: %v", w.cfg.project, w.cfg.deployment, lastErr)
		w.state.Store("error")
	})
}

// poll runs for the pod's lifetime, checking ZAD's status endpoint. As soon as the app
// is back it marks itself ready, which makes /__zad/ready return 503 so Kubernetes
// removes the waker from the Service endpoints -- the app takes over with no gap.
func (w *waker) poll(ctx context.Context) {
	url := fmt.Sprintf("%s/api/sleep-mode/%s/%s/status", w.cfg.apiURL, w.cfg.project, w.cfg.deployment)
	ticker := time.NewTicker(w.cfg.pollInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			if w.appReady.Load() {
				continue
			}
			req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
			if err != nil {
				continue
			}
			req.Header.Set("X-Wake-Token", w.cfg.token)
			resp, err := w.client.Do(req)
			if err != nil {
				continue
			}
			var body struct {
				State string `json:"state"`
			}
			_ = json.NewDecoder(resp.Body).Decode(&body)
			resp.Body.Close()
			if body.State == "ready" {
				w.appReady.Store(true)
				w.state.Store("ready")
				log.Printf("app %s/%s is back; waker stepping out of traffic", w.cfg.project, w.cfg.deployment)
			}
		}
	}
}

func (w *waker) handleHealthz(rw http.ResponseWriter, _ *http.Request) {
	rw.WriteHeader(http.StatusOK)
	_, _ = rw.Write([]byte("ok"))
}

// handleReady is deliberately inverted: 200 while the app is NOT back (so the waker
// serves traffic), 503 once it is (so the waker leaves the EndpointSlice).
func (w *waker) handleReady(rw http.ResponseWriter, _ *http.Request) {
	if w.appReady.Load() {
		rw.WriteHeader(http.StatusServiceUnavailable)
		return
	}
	rw.WriteHeader(http.StatusOK)
}

func (w *waker) handleStatus(rw http.ResponseWriter, _ *http.Request) {
	rw.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(rw).Encode(map[string]any{
		"state":       w.currentState(),
		"title":       w.cfg.title,
		"description": w.cfg.description,
		"mode":        w.cfg.mode,
		"elapsed":     int(time.Since(w.started).Seconds()),
	})
}

func (w *waker) handleWake(rw http.ResponseWriter, _ *http.Request) {
	if w.cfg.mode == "manual" {
		http.Error(rw, "waking is disabled in manual mode", http.StatusForbidden)
		return
	}
	w.wake()
	rw.WriteHeader(http.StatusAccepted)
}

func (w *waker) handleRobots(rw http.ResponseWriter, _ *http.Request) {
	rw.Header().Set("Content-Type", "text/plain")
	_, _ = rw.Write([]byte("User-agent: *\nDisallow: /\n"))
}

// handlePage serves the "application is starting" page for every other request. In
// auto mode a browser GET (not an asset, not a /__zad path) also triggers the wake.
func (w *waker) handlePage(rw http.ResponseWriter, r *http.Request) {
	if w.cfg.mode == "auto" && r.Method == http.MethodGet &&
		r.URL.Path != "/favicon.ico" && !strings.HasPrefix(r.URL.Path, "/__zad") {
		w.wake()
	}
	rw.Header().Set("X-Robots-Tag", "noindex")
	rw.Header().Set("Content-Type", "text/html; charset=utf-8")
	rw.WriteHeader(http.StatusOK)
	_ = pageTemplate.Execute(rw, map[string]any{
		"Title":       w.cfg.title,
		"Description": w.cfg.description,
		"Mode":        w.cfg.mode,
	})
}

func (w *waker) routes() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/__zad/healthz", w.handleHealthz)
	mux.HandleFunc("/__zad/ready", w.handleReady)
	mux.HandleFunc("/__zad/status", w.handleStatus)
	mux.HandleFunc("/__zad/wake", w.handleWake)
	mux.HandleFunc("/robots.txt", w.handleRobots)
	mux.HandleFunc("/", w.handlePage)
	return mux
}

func main() {
	// Size the Go runtime to the container, not the node, so the heap does not grow
	// needlessly in a 100m CPU / 64Mi pod.
	debug.SetGCPercent(50)
	debug.SetMemoryLimit(56 << 20)

	cfg := loadConfig()
	w := newWaker(cfg)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go w.poll(ctx)

	addr := ":8080"
	log.Printf("zad-waker listening on %s (project=%s deployment=%s mode=%s)", addr, cfg.project, cfg.deployment, cfg.mode)
	server := &http.Server{Addr: addr, Handler: w.routes(), ReadHeaderTimeout: 5 * time.Second}
	if err := server.ListenAndServe(); err != nil {
		log.Fatalf("server error: %v", err)
	}
}

// pageTemplate reuses the authorization sign-in card's visual language (see
// manifests/sidecar-authorization-wall.yaml.jinja) so the two pages read as one system.
var pageTemplate = template.Must(template.New("page").Parse(`<!DOCTYPE html>
<html lang="nl">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta name="robots" content="noindex">
    <title>{{ .Title }}</title>
    <style>
        *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: 'RijksoverheidSansWebText', Verdana, Arial, sans-serif;
            background-color: #F1F5F9;
            color: #1E293B;
            display: flex;
            align-items: center;
            justify-content: center;
            min-height: 100vh;
            padding: 1rem;
        }
        .card {
            background: #FFFFFF;
            border-radius: 4px;
            border: 1px solid #CBD5E1;
            padding: 2.5rem 2rem;
            max-width: 440px;
            width: 100%;
            text-align: center;
        }
        .card h1 {
            font-size: 1.25rem;
            font-weight: 700;
            color: #1E293B;
            margin-bottom: 0.75rem;
        }
        .card p {
            font-size: 0.95rem;
            color: #475569;
            line-height: 1.5;
            margin-bottom: 1.5rem;
        }
        .btn {
            display: inline-block;
            background-color: #007BC7;
            color: #FFFFFF;
            font-family: inherit;
            font-size: 1.125rem;
            font-weight: 700;
            border: 1px solid #007BC7;
            border-radius: 4px;
            padding: 0.625rem 1.5rem;
            cursor: pointer;
            text-decoration: none;
            transition: background-color 0.15s, border-color 0.15s;
        }
        .btn:hover, .btn:focus {
            background-color: #01689B;
            border-color: #01689B;
            outline: 2px solid #007BC7;
            outline-offset: 2px;
        }
        .btn[disabled] { opacity: 0.6; cursor: default; }
        .spinner {
            width: 2rem; height: 2rem; margin: 0 auto 1.25rem;
            border: 3px solid #CBD5E1; border-top-color: #007BC7; border-radius: 50%;
            animation: spin 0.9s linear infinite;
        }
        @keyframes spin { to { transform: rotate(360deg); } }
        .subtitle { font-size: 0.875rem; color: #64748B; margin-top: 1.5rem; }
        [hidden] { display: none !important; }
    </style>
</head>
<body>
    <div class="card">
        <div id="loading" hidden>
            <div class="spinner"></div>
            <h1>{{ .Title }} wordt gestart</h1>
            {{ if .Description }}<p>{{ .Description }}</p>{{ end }}
            <p>De applicatie staat in slaapstand en wordt nu opgestart. Dit kan ongeveer een minuut duren; de pagina laadt vanzelf zodra de applicatie klaar is.</p>
        </div>
        <div id="confirm" hidden>
            <h1>{{ .Title }} staat in slaapstand</h1>
            {{ if .Description }}<p>{{ .Description }}</p>{{ end }}
            <p>Deze applicatie is in slaapstand gezet om resources te sparen. Start hem op om verder te gaan; opstarten duurt ongeveer een minuut.</p>
            <button type="button" id="wake-btn" class="btn">Applicatie starten</button>
        </div>
        <div id="manual" hidden>
            <h1>{{ .Title }} staat in slaapstand</h1>
            {{ if .Description }}<p>{{ .Description }}</p>{{ end }}
            <p>Deze applicatie staat in slaapstand en moet door een beheerder worden gestart.</p>
        </div>
        <div id="error" hidden>
            <h1>Opstarten mislukt</h1>
            <p>Het is niet gelukt om de applicatie te starten. Probeer het later opnieuw of neem contact op met een beheerder.</p>
        </div>
        <p class="subtitle">Slaapstand &middot; de applicatie start koud op, sessies blijven niet bewaard.</p>
    </div>
    <script>
        var MODE = {{ .Mode }};
        function show(id) {
            ['loading', 'confirm', 'manual', 'error'].forEach(function (s) {
                document.getElementById(s).hidden = (s !== id);
            });
        }
        function startWake() {
            show('loading');
            fetch('/__zad/wake', { method: 'POST' }).catch(function () {});
        }
        if (MODE === 'manual') {
            show('manual');
        } else if (MODE === 'confirm') {
            show('confirm');
            document.getElementById('wake-btn').addEventListener('click', startWake);
        } else {
            startWake();
        }
        // Poll status; reload as soon as the app is ready, or as soon as the response is
        // no longer our JSON (which means the request already hit the real app).
        setInterval(function () {
            fetch('/__zad/status', { headers: { 'Accept': 'application/json' } })
                .then(function (r) { return r.json(); })
                .then(function (s) {
                    if (s.state === 'ready') { location.reload(); }
                    else if (s.state === 'error' && MODE !== 'manual') { show('error'); }
                })
                .catch(function () { location.reload(); });
        }, 2000);
    </script>
</body>
</html>`))
