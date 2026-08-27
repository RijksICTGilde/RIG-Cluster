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
// single-flight wake (a hundred simultaneous visitors cause exactly one wake call),
// and no questions asked on behalf of nobody (see idlePollInterval).
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

// idlePollInterval is how often the waker asks ZAD for the status while nobody is
// waiting for the answer. It is deliberately not zero: a deployment can be woken from
// outside -- zadctl, the API, the portal -- with no browser anywhere near it, and the pod
// has to find that out by itself. Its /__zad/ready is inverted (200 while the app is NOT
// back), so a pod that stopped asking would keep answering 200 and stay in the
// EndpointSlice while the app runs: a wake page in front of a woken application.
//
// The fast cadence (ZAD_POLL_INTERVAL_SEC, 3s) is what someone who clicked "start" needs;
// it applies whenever there is such a someone. See waiting.
const idlePollInterval = 30 * time.Second

// defaultPort is the fallback when ZAD_PORT is absent or unusable.
//
// The waker has no Service of its own: it joins the application's Service by carrying the
// same app label, so it has to listen on whatever port that Service targets. That port is
// the application component's, and it is 8080 for some projects and not for others -- a
// waker on the wrong port is selected by the Service, passes its own probes, and answers
// nothing, which is exactly how this went unnoticed. OPI passes the right port in ZAD_PORT;
// this default only keeps an older OPI, which passes nothing, behaving as it did.
const defaultPort = 8080

// config is the waker's runtime configuration, all from the ConfigMap/Secret env.
type config struct {
	apiURL       string
	project      string
	deployment   string
	title        string
	description  string
	mode         string        // auto | confirm | manual
	pollInterval time.Duration // while someone is waiting
	idleInterval time.Duration // while nobody is
	token        string
	port         int // the HTTP port to listen on
}

func loadConfig() config {
	poll := 3
	if v, err := strconv.Atoi(os.Getenv("ZAD_POLL_INTERVAL_SEC")); err == nil && v > 0 {
		poll = v
	}
	port := defaultPort
	// A port outside 1-65535 is not a port. Falling back beats refusing to start: the
	// waker is what a visitor sees instead of an error page, so a bad value must not turn
	// the wait into a CrashLoopBackOff.
	if v, err := strconv.Atoi(strings.TrimSpace(os.Getenv("ZAD_PORT"))); err == nil && v > 0 && v <= 65535 {
		port = v
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
		idleInterval: idlePollInterval,
		token:        os.Getenv("ZAD_WAKE_TOKEN"),
		port:         port,
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
	cfg           config
	client        *http.Client
	once          sync.Once    // single-flight: one wake call per pod lifetime
	appReady      atomic.Bool  // true once the app is back; flips /__zad/ready to 503
	state         atomic.Value // "idle" | "waking" | "ready" | "error"
	started       time.Time
	lastVisitor   atomic.Int64  // UnixNano of the last request from someone who is waiting
	wakeRequested atomic.Bool   // this pod asked ZAD to wake the deployment
	nudge         chan struct{} // wakes the poller from its slow cadence at once
}

func newWaker(cfg config) *waker {
	w := &waker{
		cfg:     cfg,
		client:  &http.Client{Timeout: 10 * time.Second},
		started: time.Now(),
		nudge:   make(chan struct{}, 1),
	}
	w.state.Store("idle")
	return w
}

func (w *waker) currentState() string {
	if s, ok := w.state.Load().(string); ok {
		return s
	}
	return "idle"
}

// waiting reports whether anyone is waiting for the status answer right now.
//
// Two situations count, and nothing else does. A visitor whose browser is on the waker
// page polls /__zad/status every two seconds, so a recent request there (or on the page
// itself) means a human is looking at a spinner. And a wake this pod asked for is in
// progress, which means the app is cold-starting while traffic arrives, so the handover
// has to be noticed the moment it happens -- even if that visitor closed the tab.
//
// A wake that FAILED is not in progress; it must not hold the fast cadence for the rest
// of the pod's life. The probes (/__zad/healthz, /__zad/ready) deliberately do not count:
// the kubelet hits those constantly, and treating them as visitors would mean the waker
// always considers someone to be waiting -- which is the situation this replaces.
func (w *waker) waiting() bool {
	if w.wakeRequested.Load() && w.currentState() != "error" {
		return true
	}
	last := w.lastVisitor.Load()
	if last == 0 {
		return false
	}
	// Three fast intervals of tolerance: a browser polls every 2s, so a single dropped
	// request does not drop the waker back to its slow cadence mid-wait.
	return time.Since(time.Unix(0, last)) < 3*w.cfg.pollInterval
}

// nextInterval is the pause before the next status call: fast while someone waits.
func (w *waker) nextInterval() time.Duration {
	if w.waiting() {
		return w.cfg.pollInterval
	}
	return w.cfg.idleInterval
}

// noteVisitor records that someone is waiting for the answer.
//
// It nudges the poller only when it was NOT already in a waiting state. That matters: the
// page polls /__zad/status every two seconds, and nudging on each of those would make the
// waker ask ZAD faster than pollInterval. Nudging on the transition instead gives the
// arriving visitor an immediate check -- so nobody waits longer than before this change
// -- and leaves the rate to the ticker after that.
func (w *waker) noteVisitor() {
	wasWaiting := w.waiting()
	w.lastVisitor.Store(time.Now().UnixNano())
	if !wasWaiting {
		w.kick()
	}
}

// kick asks the poller to check now instead of sitting out the rest of its pause.
func (w *waker) kick() {
	select {
	case w.nudge <- struct{}{}:
	default:
	}
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
		// A wake is now in progress, so the handover matters even if the visitor walks
		// away: poll at the fast cadence and check straight away.
		w.wakeRequested.Store(true)
		w.kick()
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
//
// It asks at two speeds. While someone is waiting for the answer it asks every
// pollInterval (3s), exactly as before: that person must not wait longer than they used
// to. While nobody is waiting it drops to idlePollInterval, because a sleeping deployment
// with zero visitors used to cost 1200 status calls per hour -- each one a kubectl call
// against the apiserver -- for an answer nobody read.
//
// It never stops asking. That is the point of the slow cadence rather than a pause: a
// wake started from zadctl, the API or the portal has no browser behind it, and the pod
// only learns the app is back by asking.
func (w *waker) poll(ctx context.Context) {
	url := fmt.Sprintf("%s/api/sleep-mode/%s/%s/status", w.cfg.apiURL, w.cfg.project, w.cfg.deployment)
	timer := time.NewTimer(w.nextInterval())
	defer timer.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-w.nudge:
			// A visitor arrived while the waker was on its slow cadence. Check now
			// instead of sitting out the remaining pause.
			if !timer.Stop() {
				select {
				case <-timer.C:
				default:
				}
			}
		case <-timer.C:
		}
		if !w.appReady.Load() {
			w.checkStatus(ctx, url)
		}
		timer.Reset(w.nextInterval())
	}
}

// checkStatus asks ZAD once whether the app behind the waker is back.
//
// It reads the "state" field and only that: starting | ready is the poll contract and it
// is the same in every version of ZAD (see the sleep-mode API models).
func (w *waker) checkStatus(ctx context.Context, url string) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return
	}
	req.Header.Set("X-Wake-Token", w.cfg.token)
	resp, err := w.client.Do(req)
	if err != nil {
		return
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

// handleStatus is what the waker page polls every two seconds, so a request here is the
// signal that a human is sitting in front of a spinner and the fast cadence applies.
func (w *waker) handleStatus(rw http.ResponseWriter, _ *http.Request) {
	w.noteVisitor()
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
	w.noteVisitor()
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
	if r.URL.Path != "/favicon.ico" && !strings.HasPrefix(r.URL.Path, "/__zad") {
		// Someone is at the door in every mode, including the ones that do not wake by
		// themselves: in confirm mode they are about to press the button, in manual mode
		// an admin may be waking it elsewhere right now.
		w.noteVisitor()
		if w.cfg.mode == "auto" && r.Method == http.MethodGet {
			w.wake()
		}
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

	addr := fmt.Sprintf(":%d", cfg.port)
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
