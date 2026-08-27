// Tests for the waker's two-speed poller (RC-124).
//
// The behaviour under test is a trade: a sleeping deployment that nobody visits must stop
// asking ZAD for its status 1200 times an hour, WITHOUT losing the property that the pod
// discovers by itself that the app is back. That second half is the dangerous one, and
// TestWokenFromOutside is the test that guards it -- delete the slow cadence and it hangs
// until its deadline instead of passing.
package main

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"
)

// statusServer stands in for ZAD: it counts the status calls and answers whatever the
// test currently wants the deployment's state to be.
type statusServer struct {
	*httptest.Server
	calls atomic.Int64
	ready atomic.Bool
}

func newStatusServer() *statusServer {
	s := &statusServer{}
	s.Server = httptest.NewServer(http.HandlerFunc(func(rw http.ResponseWriter, _ *http.Request) {
		s.calls.Add(1)
		state := "starting"
		if s.ready.Load() {
			state = "ready"
		}
		rw.Header().Set("Content-Type", "application/json")
		_, _ = fmt.Fprintf(rw, `{"state": %q, "sleep_state": "sleeping"}`, state)
	}))
	return s
}

// testWaker is a waker pointed at the fake ZAD, with the two cadences shrunk so a test
// takes milliseconds. mode is "confirm" so nothing wakes unless the test asks for it.
func testWaker(t *testing.T, srv *statusServer, poll, idle time.Duration) *waker {
	t.Helper()
	w := newWaker(config{
		apiURL:       srv.URL,
		project:      "demo",
		deployment:   "web",
		mode:         "confirm",
		pollInterval: poll,
		idleInterval: idle,
	})
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	go w.poll(ctx)
	return w
}

// waitFor polls a local condition until it holds, or fails after the deadline.
func waitFor(t *testing.T, deadline time.Duration, what string, cond func() bool) {
	t.Helper()
	stop := time.Now().Add(deadline)
	for time.Now().Before(stop) {
		if cond() {
			return
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatalf("timed out after %s waiting for %s", deadline, what)
}

func readyCode(t *testing.T, w *waker) int {
	t.Helper()
	rec := httptest.NewRecorder()
	w.handleReady(rec, httptest.NewRequest(http.MethodGet, "/__zad/ready", nil))
	return rec.Code
}

// TestWokenFromOutside is the core of RC-124: a deployment woken via zadctl, the API or
// the portal has no browser behind it, so the pod is never visited. It still has to notice
// the app is back and take itself out of the EndpointSlice -- its /__zad/ready is inverted
// (200 while the app is NOT back), so a pod that stopped asking would serve a wake page in
// front of a running application forever.
func TestWokenFromOutside(t *testing.T) {
	srv := newStatusServer()
	defer srv.Close()
	// A long fast interval and a short idle one: only the idle cadence can carry this
	// test. If the slow poll is removed (stop instead of slow down), nothing asks and
	// the waker never leaves traffic.
	w := testWaker(t, srv, time.Hour, 20*time.Millisecond)

	if got := readyCode(t, w); got != http.StatusOK {
		t.Fatalf("waker should serve traffic while the app is down, got ready=%d", got)
	}

	// The wake happens entirely outside this pod. No request ever reaches the waker.
	srv.ready.Store(true)

	waitFor(t, 2*time.Second, "the waker to notice an outside wake", func() bool {
		return w.appReady.Load()
	})
	if got := readyCode(t, w); got != http.StatusServiceUnavailable {
		t.Fatalf("waker should have left the EndpointSlice, got ready=%d", got)
	}
	if w.currentState() != "ready" {
		t.Fatalf("state should be ready, got %q", w.currentState())
	}
}

// TestIdleCadenceIsSlow pins the saving itself: with nobody waiting, the gap between two
// status calls is the idle interval and not the fast one.
func TestIdleCadenceIsSlow(t *testing.T) {
	srv := newStatusServer()
	defer srv.Close()
	poll, idle := 5*time.Millisecond, 400*time.Millisecond
	w := testWaker(t, srv, poll, idle)

	if w.waiting() {
		t.Fatal("a pod nobody has visited should not think someone is waiting")
	}
	if got := w.nextInterval(); got != idle {
		t.Fatalf("idle pod should use the idle interval, got %s", got)
	}

	waitFor(t, 2*time.Second, "the first status call", func() bool { return srv.calls.Load() >= 1 })
	first := time.Now()
	waitFor(t, 3*time.Second, "the second status call", func() bool { return srv.calls.Load() >= 2 })
	// Generous margin: this asserts "not the fast cadence", not the exact clock.
	if gap := time.Since(first); gap < idle/2 {
		t.Fatalf("second call came after %s, which is the fast cadence, not the idle one", gap)
	}
}

// TestVisitorGetsTheFastCadence is the other half of the trade: whoever is actually
// waiting must not wait longer than before. A visitor's first status poll both switches
// the cadence and triggers an immediate check, so no part of the idle pause is added to
// their wait.
func TestVisitorGetsTheFastCadence(t *testing.T) {
	srv := newStatusServer()
	defer srv.Close()
	// An idle interval far longer than the test: every call it makes is one the visitor
	// caused.
	w := testWaker(t, srv, 10*time.Millisecond, time.Hour)

	// The browser on the waker page polls /__zad/status.
	rec := httptest.NewRecorder()
	w.handleStatus(rec, httptest.NewRequest(http.MethodGet, "/__zad/status", nil))

	if !w.waiting() {
		t.Fatal("a status poll from the page means someone is waiting")
	}
	if got := w.nextInterval(); got != 10*time.Millisecond {
		t.Fatalf("a waiting visitor should get the fast interval, got %s", got)
	}
	waitFor(t, time.Second, "the visitor's immediate check", func() bool { return srv.calls.Load() >= 1 })
	waitFor(t, time.Second, "the fast cadence to continue", func() bool { return srv.calls.Load() >= 3 })

	srv.ready.Store(true)
	waitFor(t, 2*time.Second, "the app to be handed over", func() bool { return w.appReady.Load() })
}

// TestPageVisitCountsAsWaiting: the visitor's very first request is the page itself, not
// the status poll, and it has to switch the cadence too.
func TestPageVisitCountsAsWaiting(t *testing.T) {
	srv := newStatusServer()
	defer srv.Close()
	w := testWaker(t, srv, 10*time.Millisecond, time.Hour)

	rec := httptest.NewRecorder()
	w.handlePage(rec, httptest.NewRequest(http.MethodGet, "/some/deep/link", nil))

	if !w.waiting() {
		t.Fatal("a page view means someone is waiting")
	}
	waitFor(t, time.Second, "a status call caused by the page view", func() bool { return srv.calls.Load() >= 1 })
}

// TestProbesAreNotVisitors: the kubelet hits /__zad/ready and /__zad/healthz constantly.
// If those counted, the waker would always believe someone is waiting and nothing would
// be saved at all.
func TestProbesAreNotVisitors(t *testing.T) {
	srv := newStatusServer()
	defer srv.Close()
	w := testWaker(t, srv, 10*time.Millisecond, time.Hour)

	for i := 0; i < 5; i++ {
		w.handleReady(httptest.NewRecorder(), httptest.NewRequest(http.MethodGet, "/__zad/ready", nil))
		w.handleHealthz(httptest.NewRecorder(), httptest.NewRequest(http.MethodGet, "/__zad/healthz", nil))
	}
	if w.waiting() {
		t.Fatal("kubelet probes are not someone waiting for the answer")
	}
	if got := w.nextInterval(); got != time.Hour {
		t.Fatalf("probes should leave the idle cadence alone, got %s", got)
	}
}

// TestWaitingExpires: a visitor who closes the tab stops being someone who waits, so the
// pod returns to its slow cadence instead of polling fast for the rest of its life.
func TestWaitingExpires(t *testing.T) {
	w := newWaker(config{pollInterval: 5 * time.Millisecond, idleInterval: time.Minute})

	w.noteVisitor()
	if !w.waiting() {
		t.Fatal("a fresh visitor should be waiting")
	}
	// The window is three fast intervals; past that the tab is gone.
	w.lastVisitor.Store(time.Now().Add(-time.Second).UnixNano())
	if w.waiting() {
		t.Fatal("a visitor from a second ago should have expired at a 5ms interval")
	}
	if got := w.nextInterval(); got != time.Minute {
		t.Fatalf("expired visitor should return the pod to the idle cadence, got %s", got)
	}
}

// TestWakeInFlightKeepsTheFastCadence: after a wake the app is cold-starting and traffic
// is arriving, so the handover has to be seen at once even if the visitor walked away.
// A wake that FAILED is not in progress and must not hold the fast cadence forever.
func TestWakeInFlightKeepsTheFastCadence(t *testing.T) {
	w := newWaker(config{pollInterval: 5 * time.Millisecond, idleInterval: time.Minute})

	w.wakeRequested.Store(true)
	w.state.Store("waking")
	if !w.waiting() {
		t.Fatal("a wake in progress means the answer is being waited for")
	}
	if got := w.nextInterval(); got != 5*time.Millisecond {
		t.Fatalf("a wake in progress should poll fast, got %s", got)
	}

	w.state.Store("error")
	if w.waiting() {
		t.Fatal("a failed wake is not in progress and should release the fast cadence")
	}
}

// TestDefaultIdleInterval: loadConfig fills the idle cadence in, so a ConfigMap that only
// sets ZAD_POLL_INTERVAL_SEC (which is all OPI writes) still gets the slow poll.
func TestDefaultIdleInterval(t *testing.T) {
	t.Setenv("ZAD_POLL_INTERVAL_SEC", "3")
	cfg := loadConfig()

	if cfg.pollInterval != 3*time.Second {
		t.Fatalf("fast interval should come from the env, got %s", cfg.pollInterval)
	}
	if cfg.idleInterval != idlePollInterval {
		t.Fatalf("idle interval should default to %s, got %s", idlePollInterval, cfg.idleInterval)
	}
	if cfg.idleInterval <= cfg.pollInterval {
		t.Fatal("the idle cadence must be slower than the fast one, or nothing is saved")
	}
}

// TestListenPortComesFromTheEnv: the waker joins the application's Service, so it has to
// listen on the port that Service targets. Hardcoding 8080 made the whole feature work
// only for applications that happen to use 8080; for the others the Service selected a
// healthy pod that answered nothing.
func TestListenPortComesFromTheEnv(t *testing.T) {
	t.Setenv("ZAD_PORT", "8000")

	if got := loadConfig().port; got != 8000 {
		t.Fatalf("port should come from ZAD_PORT, got %d", got)
	}
}

// TestListenPortFallsBackTo8080: an OPI that does not pass ZAD_PORT yet must keep the
// behaviour it has today. This is what makes publishing this image ahead of the OPI change
// a no-op rather than a change in production.
func TestListenPortFallsBackTo8080(t *testing.T) {
	if got := loadConfig().port; got != defaultPort {
		t.Fatalf("without ZAD_PORT the waker should stay on %d, got %d", defaultPort, got)
	}
}

// TestSurroundingWhitespaceIsIgnored: the same courtesy ZAD_WAKE_MODE already gets. A
// value that plainly means 8000 must not silently land on the fallback -- listening on the
// wrong port is the failure this whole change is about.
func TestSurroundingWhitespaceIsIgnored(t *testing.T) {
	t.Setenv("ZAD_PORT", "  8000\n")

	if got := loadConfig().port; got != 8000 {
		t.Fatalf("a padded ZAD_PORT should still be read, got %d", got)
	}
}

// TestAnUnusablePortFallsBackInsteadOfFailing: the waker is what a visitor sees instead of
// an error page. A typo in the value must not turn that into a CrashLoopBackOff.
func TestAnUnusablePortFallsBackInsteadOfFailing(t *testing.T) {
	for _, value := range []string{"", "http", "0", "-1", "65536"} {
		t.Run(value, func(t *testing.T) {
			t.Setenv("ZAD_PORT", value)

			if got := loadConfig().port; got != defaultPort {
				t.Fatalf("ZAD_PORT=%q should fall back to %d, got %d", value, defaultPort, got)
			}
		})
	}
}
