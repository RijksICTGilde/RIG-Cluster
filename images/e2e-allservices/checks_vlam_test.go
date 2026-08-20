package main

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// The vlam probe is the only check whose verdict is not simply "did it round-trip".
// It has to tell three things apart that all arrive as an HTTP answer: the chain works
// (200 with a models document), the chain works and VLAM refuses the caller (401/403,
// which is still a pass because only VLAM can produce it), and the chain is broken
// (nothing, an error status, or an answer that did not come from VLAM). These tests pin
// exactly those boundaries, since a probe that passes on the wrong ones is worse than
// no probe at all.

// serve starts a test server, points VLAM_API_URL at it and returns the last path it saw.
func serve(t *testing.T, handler http.HandlerFunc) *string {
	t.Helper()
	seen := new(string)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		*seen = r.URL.Path
		handler(w, r)
	}))
	t.Cleanup(server.Close)
	t.Setenv("VLAM_API_URL", server.URL)
	return seen
}

func jsonHandler(status int, body string) http.HandlerFunc {
	return func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		_, _ = w.Write([]byte(body))
	}
}

func TestVlamModelsDocumentPasses(t *testing.T) {
	seen := serve(t, jsonHandler(http.StatusOK, `{"data":[{"id":"vlam-stub","object":"model"}],"object":"list"}`))

	result := checkVlam(context.Background())

	if !result.ok || result.err != nil {
		t.Fatalf("expected ok, got ok=%v err=%v", result.ok, result.err)
	}
	if *seen != vlamModelsPath {
		t.Errorf("probe called %q, expected %q", *seen, vlamModelsPath)
	}
	if result.detail["models"] != 1 {
		t.Errorf("expected models=1 in detail, got %v", result.detail["models"])
	}
	if result.detail["first_model"] != "vlam-stub" {
		t.Errorf("expected the model id in detail, got %v", result.detail["first_model"])
	}
}

func TestVlamEmptyModelListStillPasses(t *testing.T) {
	// An empty catalogue is VLAM's answer, not a broken chain.
	serve(t, jsonHandler(http.StatusOK, `{"data":[],"object":"list"}`))

	result := checkVlam(context.Background())

	if !result.ok || result.err != nil {
		t.Fatalf("expected ok, got ok=%v err=%v", result.ok, result.err)
	}
	if result.detail["models"] != 0 {
		t.Errorf("expected models=0 in detail, got %v", result.detail["models"])
	}
}

func TestVlamRefusalPasses(t *testing.T) {
	// 401/403 can only come from VLAM itself: the path stands, the key does not.
	for _, status := range []int{http.StatusUnauthorized, http.StatusForbidden} {
		serve(t, jsonHandler(status, `{"error":"no key"}`))

		result := checkVlam(context.Background())

		if !result.ok || result.err != nil {
			t.Errorf("status %d: expected ok, got ok=%v err=%v", status, result.ok, result.err)
		}
		if verdict, _ := result.detail["verdict"].(string); !strings.Contains(verdict, "refuses") {
			t.Errorf("status %d: expected the verdict to record the refusal, got %q", status, verdict)
		}
	}
}

func TestVlamServerErrorFailsAndNamesTheProxy(t *testing.T) {
	serve(t, jsonHandler(http.StatusBadGateway, "upstream gone"))

	result := checkVlam(context.Background())

	if result.ok || result.err == nil {
		t.Fatalf("expected a failure, got ok=%v err=%v", result.ok, result.err)
	}
	if !strings.Contains(result.err.Error(), "proxy") {
		t.Errorf("a 5xx should point at the proxy/upstream hop, got %q", result.err)
	}
}

func TestVlamNotFoundFails(t *testing.T) {
	serve(t, jsonHandler(http.StatusNotFound, "no such path"))

	result := checkVlam(context.Background())

	if result.ok || result.err == nil {
		t.Fatalf("expected a failure, got ok=%v err=%v", result.ok, result.err)
	}
}

func TestVlamNonJSONAnswerFails(t *testing.T) {
	// Something answered on the address, but it was not VLAM. A probe that accepted
	// any 200 would call a proxy error page a working chain.
	serve(t, func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/html")
		_, _ = w.Write([]byte("<html><body>503 Service Unavailable</body></html>"))
	})

	result := checkVlam(context.Background())

	if result.ok || result.err == nil {
		t.Fatalf("expected a failure, got ok=%v err=%v", result.ok, result.err)
	}
	if !strings.Contains(result.err.Error(), "models document") {
		t.Errorf("expected the error to say the answer was not a models document, got %q", result.err)
	}
}

func TestVlamJSONWithoutDataFails(t *testing.T) {
	serve(t, jsonHandler(http.StatusOK, `{"object":"list"}`))

	result := checkVlam(context.Background())

	if result.ok || result.err == nil {
		t.Fatalf("expected a failure, got ok=%v err=%v", result.ok, result.err)
	}
}

func TestVlamNoAnswerNamesTheNetworkHops(t *testing.T) {
	// Nothing listening: the shape of a closed egress policy or a missing proxy.
	server := httptest.NewServer(http.NotFoundHandler())
	address := server.URL
	server.Close()
	t.Setenv("VLAM_API_URL", address)

	result := checkVlam(context.Background())

	if result.ok || result.err == nil {
		t.Fatalf("expected a failure, got ok=%v err=%v", result.ok, result.err)
	}
	message := result.err.Error()
	if !strings.Contains(message, "egress") || !strings.Contains(message, "inbound") {
		t.Errorf("a connection failure should name the egress and inbound hops, got %q", message)
	}
}

func TestVlamUsesTheAppAliasAndTrimsTheTrailingSlash(t *testing.T) {
	seen := new(string)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		*seen = r.URL.Path
		_, _ = w.Write([]byte(`{"data":[]}`))
	}))
	t.Cleanup(server.Close)
	t.Setenv("VLAM_API_URL", "")
	t.Setenv("APP_VLAM_API_URL", server.URL+"/")

	result := checkVlam(context.Background())

	if !result.ok || result.err != nil {
		t.Fatalf("expected ok, got ok=%v err=%v", result.ok, result.err)
	}
	if *seen != vlamModelsPath {
		t.Errorf("probe called %q, expected %q (a trailing slash must not double up)", *seen, vlamModelsPath)
	}
}

func TestVlamWithoutAnAddressFails(t *testing.T) {
	t.Setenv("VLAM_API_URL", "")
	t.Setenv("APP_VLAM_API_URL", "")

	result := checkVlam(context.Background())

	if result.ok || result.err == nil {
		t.Fatalf("expected a failure, got ok=%v err=%v", result.ok, result.err)
	}
}
