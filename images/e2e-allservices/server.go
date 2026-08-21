package main

import (
	"encoding/json"
	"fmt"
	"html"
	"net/http"
	"net/url"
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

	// Manual, human-triggered: a real send eats from the project's daily budget on
	// the relay, so this is a button on the page, never part of the check round.
	// POST-redirect-GET so a refresh does not resend.
	mux.HandleFunc("/send-testmail", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "POST only", http.StatusMethodNotAllowed)
			return
		}
		to := strings.TrimSpace(r.FormValue("to"))
		if to == "" || !strings.Contains(to, "@") {
			http.Redirect(w, r, "/?testmailerr="+url.QueryEscape("vul een geldig ontvangeradres in"), http.StatusSeeOther)
			return
		}
		subject, err := sendTestMail(to)
		if err != nil {
			logInfo("testmail to %s failed: %v", to, err)
			http.Redirect(w, r, "/?testmailerr="+url.QueryEscape(err.Error()), http.StatusSeeOther)
			return
		}
		http.Redirect(w, r, "/?testmail="+url.QueryEscape(subject), http.StatusSeeOther)
	})

	// Manual, human-triggered as well, and for the same reason: the periodic vlam probe
	// carries no credential, so only a human with a token can prove the last stretch of
	// the chain. Unlike the testmail button this answers in place instead of redirecting:
	// the answer would otherwise travel through a query string and land in every access
	// log between here and the browser, and the answer is exactly what must not be logged.
	mux.HandleFunc("/vlam-chat", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "POST only", http.StatusMethodNotAllowed)
			return
		}
		// No binding, no button, so no endpoint either - and this is also what keeps the
		// handler from being a destination-less POST target on a deployment without vlam.
		if !vlamBound() {
			http.NotFound(w, r)
			return
		}
		token := strings.TrimSpace(r.FormValue("token"))
		outcome := &chatOutcome{
			Model:    strings.TrimSpace(r.FormValue("model")),
			Question: strings.TrimSpace(r.FormValue("question")),
		}
		switch {
		case token == "":
			outcome.Err = "vul een token in"
		case outcome.Model == "":
			outcome.Err = "vul een modelnaam in"
		case outcome.Question == "":
			outcome.Err = "vul een vraag in"
		default:
			answer, err := vlamChat(r.Context(), token, outcome.Model, outcome.Question)
			if err != nil {
				outcome.Err = err.Error()
			} else {
				outcome.Answer = answer
			}
		}
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		_, _ = w.Write([]byte(renderHTML(cache, r.URL.Query(), outcome)))
	})

	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/" {
			http.NotFound(w, r)
			return
		}
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		_, _ = w.Write([]byte(renderHTML(cache, r.URL.Query(), nil)))
	})

	return mux
}

// chatOutcome is what the last VLAM chat attempt produced, for rendering back onto
// the page. It carries the model and the question so the form keeps what was typed -
// and deliberately not the token, which is never shown again after it is submitted.
type chatOutcome struct {
	Model    string
	Question string
	Answer   string
	Err      string
}

// renderHTML is a single self-contained page: a Hello, world banner plus a live
// table of service -> OK/FAIL/skipped, so the workload is eyeball-able in a
// browser via the project's public ingress. When the send-email service is bound
// it also carries the manual testmail form; query holds the outcome of the last
// send (set by the /send-testmail redirect). When the vlam service is bound it
// carries the chat form too; chat is the outcome of the POST being answered, or
// nil on a plain GET.
func renderHTML(cache *resultCache, query url.Values, chat *chatOutcome) string {
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

	var mailSection string
	if mailBound() {
		outcome := ""
		if s := query.Get("testmail"); s != "" {
			// "Aangenomen" en niet "verstuurd", want dat is het enige dat hier bekend is:
			// de relay heeft het bericht in zijn wachtrij gezet. De bezorging gebeurt
			// daarna en kan alsnog stranden. GEMETEN op productie 21 augustus 2026: een
			// bericht naar een extern adres werd hier als succes gemeld en 131 seconden
			// later door de upstream geweigerd met "550 #5.1.0 Address rejected", zonder
			// dat iemand dat te zien kreeg (zie plans/mail-vervolgpunten.md, punt 8 en 10).
			// De sink werd hier onvoorwaardelijk genoemd terwijl die alleen op local en
			// sandboxed-local bestaat; op productie gaat post naar de echte upstream.
			outcome = fmt.Sprintf(`<p style="color:#137333">Aangenomen door de relay: <code>%s</code> &mdash; een ontvangstbevestiging, geen bezorgbewijs. Zoek het onderwerp bij de ontvanger.</p>`, html.EscapeString(s))
		} else if e := query.Get("testmailerr"); e != "" {
			outcome = fmt.Sprintf(`<p style="color:#c5221f">Mislukt: %s</p>`, html.EscapeString(e))
		}
		mailSection = fmt.Sprintf(`
<h2>Testmail</h2>
<p class="meta">Verstuurt echt een bericht via de mailrelay (STARTTLS + AUTH, telt mee in het dagbudget) als account <code>%s</code>.</p>
<form method="post" action="/send-testmail">
 <input type="email" name="to" value="test@example.com" size="32" required>
 <button type="submit">Stuur testmail</button>
</form>%s`, html.EscapeString(firstEnv("SMTP_USERNAME")), outcome)
	}

	vlamSection := renderVlamSection(cache, chat)

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
 label{display:inline-block;min-width:5rem}
 pre{background:#f7f7f7;border:1px solid #ddd;padding:.7rem;max-width:48rem;white-space:pre-wrap;overflow-wrap:anywhere}
</style></head>
<body>
<h1>Hello, world</h1>
<p class="meta">e2e-allservices probe &mdash; deployment <code>%s</code>, component <code>%s</code></p>
<p>Overall: <span class="badge">%s</span></p>
<table>
<thead><tr><th>service</th><th>kind</th><th>verdict</th><th>latency</th><th>error</th></tr></thead>
<tbody>%s</tbody>
</table>
%s
%s
<p class="meta">Machine-readable status at <a href="/status">/status</a>; liveness at <a href="/healthz">/healthz</a>.</p>
</body></html>`,
		html.EscapeString(status.Deployment), html.EscapeString(status.Component),
		html.EscapeString(overall), rows.String(), mailSection, vlamSection)
}

// renderVlamSection is the "Test VLAM" block: empty unless the vlam service is bound.
// The model field is pre-filled with what the last probe round saw, so the common case
// is one click. The token field is a password field and never gets a value back - not
// even after a POST that used it.
func renderVlamSection(cache *resultCache, chat *chatOutcome) string {
	if !vlamBound() {
		return ""
	}
	endpoint := strings.TrimRight(firstEnv("VLAM_API_URL"), "/") + vlamChatPath

	model, question := vlamLastModel(cache), defaultChatQuestion
	outcome := ""
	if chat != nil {
		if chat.Model != "" {
			model = chat.Model
		}
		if chat.Question != "" {
			question = chat.Question
		}
		switch {
		case chat.Err != "":
			outcome = fmt.Sprintf(`<p style="color:#c5221f">Mislukt: %s</p>`, html.EscapeString(chat.Err))
		default:
			outcome = fmt.Sprintf(`<p style="color:#137333">Antwoord van <code>%s</code>:</p><pre>%s</pre>`,
				html.EscapeString(chat.Model), html.EscapeString(chat.Answer))
		}
	}

	return fmt.Sprintf(`
<h2>Test VLAM</h2>
<p class="meta">Doet echt een chat-completion via <code>%s</code>, met het token dat je hier invult.
Dat token gaat alleen mee in die ene aanroep: het wordt niet opgeslagen, niet gelogd en niet teruggetoond.
De vraag en het antwoord komen alleen op deze pagina, niet in de log.</p>
<form method="post" action="/vlam-chat">
 <p><label for="vlam-token">Token</label>
  <input id="vlam-token" type="password" name="token" size="40" autocomplete="off" required></p>
 <p><label for="vlam-model">Model</label>
  <input id="vlam-model" type="text" name="model" value="%s" size="40" required></p>
 <p><label for="vlam-question">Vraag</label>
  <input id="vlam-question" type="text" name="question" value="%s" size="40" required></p>
 <button type="submit">Stel de vraag</button>
</form>%s`,
		html.EscapeString(endpoint), html.EscapeString(model), html.EscapeString(question), outcome)
}
