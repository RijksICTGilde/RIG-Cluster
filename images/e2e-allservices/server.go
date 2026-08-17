package main

import (
	"encoding/json"
	"fmt"
	"html"
	"net/http"
	"strings"
)

// statusResponse is the JSON body served at /status, the payload E2E asserts on.
type statusResponse struct {
	Deployment string            `json:"deployment"`
	Component  string            `json:"component"`
	Ready      bool              `json:"ready"`
	AllOK      bool              `json:"all_ok"`
	Services   map[string]Result `json:"services"`
}

func buildStatus(cache *resultCache) statusResponse {
	results, ready, allOK := cache.snapshot()
	services := make(map[string]Result, len(results))
	for _, r := range results {
		services[r.ID] = r
	}
	return statusResponse{
		Deployment: firstEnv("DEPLOYMENT_NAME"),
		Component:  firstEnv("COMPONENT_NAME"),
		Ready:      ready,
		AllOK:      allOK,
		Services:   services,
	}
}

func newMux(cache *resultCache) *http.ServeMux {
	mux := http.NewServeMux()

	// Liveness only: 200 as soon as the process serves. Never reflects a
	// downstream service, matching the platform's tcpSocket:8080 probe intent.
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/plain")
		_, _ = w.Write([]byte("ok\n"))
	})

	mux.HandleFunc("/status", func(w http.ResponseWriter, r *http.Request) {
		status := buildStatus(cache)
		code := http.StatusOK
		// Optional strict mode: 503 when not all bound services verify. Off by
		// default so tests can read the body and assert per service.
		if r.URL.Query().Get("strict") == "1" && (!status.Ready || !status.AllOK) {
			code = http.StatusServiceUnavailable
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(code)
		enc := json.NewEncoder(w)
		enc.SetIndent("", "  ")
		_ = enc.Encode(status)
	})

	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/" {
			http.NotFound(w, r)
			return
		}
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		_, _ = w.Write([]byte(renderHTML(cache)))
	})

	return mux
}

// renderHTML is a single self-contained page: a Hello, world banner plus a live
// table of service -> OK/FAIL/skipped, so the workload is eyeball-able in a
// browser via the project's public ingress.
func renderHTML(cache *resultCache) string {
	status := buildStatus(cache)
	var rows strings.Builder
	results, _, _ := cache.snapshot()
	for _, r := range results {
		verdict := "skipped"
		color := "#888"
		switch {
		case !r.Bound:
			verdict, color = "skipped", "#888"
		case r.OK != nil && *r.OK:
			verdict, color = "OK", "#137333"
		default:
			verdict, color = "FAIL", "#c5221f"
		}
		detail := ""
		if r.Error != "" {
			detail = html.EscapeString(r.Error)
		}
		rows.WriteString(fmt.Sprintf(
			"<tr><td>%s</td><td>%s</td><td style=\"color:%s;font-weight:600\">%s</td><td>%dms</td><td>%s</td></tr>",
			html.EscapeString(r.ID), html.EscapeString(r.Kind), color, verdict, r.LatencyMS, detail,
		))
	}

	overall := "verifying..."
	if status.Ready {
		if status.AllOK {
			overall = "ALL OK"
		} else {
			overall = "FAILURES"
		}
	}

	return fmt.Sprintf(`<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>e2e-allservices</title>
<style>
 body{font-family:system-ui,sans-serif;margin:2rem;color:#1a1a1a}
 h1{font-size:2rem}
 table{border-collapse:collapse;margin-top:1rem;min-width:36rem}
 th,td{border:1px solid #ddd;padding:.4rem .7rem;text-align:left;font-size:.95rem}
 th{background:#f3f3f3}
 .meta{color:#555;margin-top:.3rem}
 .badge{display:inline-block;padding:.2rem .6rem;border-radius:.4rem;background:#eef;font-weight:600}
</style></head>
<body>
<h1>Hello, world</h1>
<p class="meta">e2e-allservices probe &mdash; deployment <code>%s</code>, component <code>%s</code></p>
<p>Overall: <span class="badge">%s</span></p>
<table>
<thead><tr><th>service</th><th>kind</th><th>verdict</th><th>latency</th><th>error</th></tr></thead>
<tbody>%s</tbody>
</table>
<p class="meta">Machine-readable status at <a href="/status">/status</a>; liveness at <a href="/healthz">/healthz</a>.</p>
</body></html>`,
		html.EscapeString(status.Deployment), html.EscapeString(status.Component),
		html.EscapeString(overall), rows.String())
}
