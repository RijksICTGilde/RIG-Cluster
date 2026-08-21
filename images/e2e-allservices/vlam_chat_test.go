package main

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"strings"
	"testing"
)

// The chat button is the only place in this image where a SECRET travels through a form,
// so these tests pin two things at once. First the same thing the probe tests pin: an
// answer that arrives has to be told apart from an answer that only looks like one, and
// each failure has to name the hop that is suspect - which for this call differs from the
// probe's, because a token is now in play. Second, and the reason a plain "it works" test
// is not enough here: the token must not survive the request. Not in the page that comes
// back, not in the form, and not in a log line - a status page is public and this pod's
// log is readable by anyone with kubectl.

// The token every test posts. Distinctive so a substring search for it is meaningful.
const testToken = "sk-geheimtoken-abc123"

const chatAnswer = "Ja, de verbinding werkt."

func chatBody(content string) string {
	body, _ := json.Marshal(map[string]any{
		"id":     "chatcmpl-test",
		"object": "chat.completion",
		"choices": []map[string]any{
			{"index": 0, "message": map[string]string{"role": "assistant", "content": content}},
		},
	})
	return string(body)
}

// chatServe points VLAM_API_URL at a test server and returns the request it saw.
type seenRequest struct {
	path  string
	auth  string
	body  string
	count int
}

func chatServe(t *testing.T, handler http.HandlerFunc) *seenRequest {
	t.Helper()
	seen := &seenRequest{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		seen.path, seen.auth, seen.body = r.URL.Path, r.Header.Get("Authorization"), string(raw)
		seen.count++
		handler(w, r)
	}))
	t.Cleanup(server.Close)
	t.Setenv("VLAM_API_URL", server.URL)
	return seen
}

func TestVlamChatAsksTheQuestionAndReturnsTheAnswer(t *testing.T) {
	seen := chatServe(t, jsonHandler(http.StatusOK, chatBody(chatAnswer)))

	answer, err := vlamChat(context.Background(), testToken, "vlam-stub", "werkt dit?")

	if err != nil {
		t.Fatalf("expected an answer, got %v", err)
	}
	if answer != chatAnswer {
		t.Errorf("expected the content of the first choice, got %q", answer)
	}
	if seen.path != vlamChatPath {
		t.Errorf("posted to %q, expected %q", seen.path, vlamChatPath)
	}
	if seen.auth != "Bearer "+testToken {
		t.Errorf("expected the token as a bearer credential, got %q", seen.auth)
	}
	var sent chatRequest
	if err := json.Unmarshal([]byte(seen.body), &sent); err != nil {
		t.Fatalf("the request body was not JSON: %v (%q)", err, seen.body)
	}
	if sent.Model != "vlam-stub" || sent.Stream {
		t.Errorf("expected model=vlam-stub and no streaming, got %+v", sent)
	}
	if len(sent.Messages) != 1 || sent.Messages[0].Role != "user" || sent.Messages[0].Content != "werkt dit?" {
		t.Errorf("expected one user message carrying the question, got %+v", sent.Messages)
	}
}

func TestVlamChatRefusalNamesTheToken(t *testing.T) {
	// The chain stands - only VLAM can answer 401/403 - so the token is what is left.
	for _, status := range []int{http.StatusUnauthorized, http.StatusForbidden} {
		chatServe(t, jsonHandler(status, `{"error":"invalid api key"}`))

		_, err := vlamChat(context.Background(), testToken, "vlam-stub", "werkt dit?")

		if err == nil {
			t.Fatalf("status %d: expected a failure", status)
		}
		if !strings.Contains(err.Error(), "the token") {
			t.Errorf("status %d: a refusal should point at the token, got %q", status, err)
		}
	}
}

func TestVlamChatRejectedRequestNamesTheModel(t *testing.T) {
	// A model that does not exist arrives as a 404 or a 400 depending on the far end, so
	// both have to send the reader to the model field rather than to the network.
	for _, status := range []int{http.StatusNotFound, http.StatusBadRequest, http.StatusUnprocessableEntity} {
		chatServe(t, jsonHandler(status, `{"error":"model not found"}`))

		_, err := vlamChat(context.Background(), testToken, "typefout-model", "werkt dit?")

		if err == nil {
			t.Fatalf("status %d: expected a failure", status)
		}
		if !strings.Contains(err.Error(), "typefout-model") {
			t.Errorf("status %d: expected the rejected model name in the message, got %q", status, err)
		}
	}
}

func TestVlamChatServerErrorNamesTheProxy(t *testing.T) {
	chatServe(t, jsonHandler(http.StatusBadGateway, "upstream gone"))

	_, err := vlamChat(context.Background(), testToken, "vlam-stub", "werkt dit?")

	if err == nil || !strings.Contains(err.Error(), "proxy") {
		t.Errorf("a 5xx should point at the proxy/upstream hop, got %v", err)
	}
}

func TestVlamChatNoAnswerNamesTheNetworkPath(t *testing.T) {
	// Nothing listening: the shape of a closed egress policy or a missing proxy. This is
	// the one failure that is NOT about the token, and it has to say so.
	server := httptest.NewServer(http.NotFoundHandler())
	address := server.URL
	server.Close()
	t.Setenv("VLAM_API_URL", address)

	_, err := vlamChat(context.Background(), testToken, "vlam-stub", "werkt dit?")

	if err == nil {
		t.Fatal("expected a failure")
	}
	if !strings.Contains(err.Error(), "egress") || !strings.Contains(err.Error(), "inbound") {
		t.Errorf("a connection failure should name the network hops, got %q", err)
	}
}

func TestVlamChatNonChatAnswerFails(t *testing.T) {
	// A 200 from something that is not VLAM (a proxy error page, or the models document
	// on the wrong path) must not be reported as a working chain.
	for _, body := range []string{"<html>503 Service Unavailable</html>", `{"data":[]}`, `{"choices":[]}`} {
		chatServe(t, jsonHandler(http.StatusOK, body))

		_, err := vlamChat(context.Background(), testToken, "vlam-stub", "werkt dit?")

		if err == nil {
			t.Fatalf("body %q: expected a failure", body)
		}
		if !strings.Contains(err.Error(), "chat completion") {
			t.Errorf("body %q: expected the error to say it was not a chat completion, got %q", body, err)
		}
	}
}

func TestVlamChatTrimsTheTrailingSlash(t *testing.T) {
	seen := &seenRequest{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seen.path = r.URL.Path
		_, _ = w.Write([]byte(chatBody(chatAnswer)))
	}))
	t.Cleanup(server.Close)
	t.Setenv("VLAM_API_URL", server.URL+"/")

	if _, err := vlamChat(context.Background(), testToken, "vlam-stub", "werkt dit?"); err != nil {
		t.Fatalf("expected an answer, got %v", err)
	}
	if seen.path != vlamChatPath {
		t.Errorf("posted to %q, expected %q (a trailing slash must not double up)", seen.path, vlamChatPath)
	}
}

func TestVlamChatWithoutAnAddressFails(t *testing.T) {
	t.Setenv("VLAM_API_URL", "")

	if _, err := vlamChat(context.Background(), testToken, "vlam-stub", "werkt dit?"); err == nil {
		t.Fatal("expected a failure when the service is not bound")
	}
}

func TestVlamChatStripsTheTokenFromAQuotedErrorBody(t *testing.T) {
	// Not a hypothetical worth ignoring: an API gateway that echoes the request it
	// rejected would otherwise put the token straight back on a public page.
	chatServe(t, jsonHandler(http.StatusBadRequest, `{"error":"bad request: Authorization: Bearer `+testToken+`"}`))

	_, err := vlamChat(context.Background(), testToken, "vlam-stub", "werkt dit?")

	if err == nil {
		t.Fatal("expected a failure")
	}
	if strings.Contains(err.Error(), testToken) {
		t.Errorf("the token came back inside the error message: %q", err)
	}
}

func TestVlamChatStripsTheTokenFromTheAnswerItself(t *testing.T) {
	// The mirror image of the test above, and just as real: ask a model to repeat its
	// context and the token comes back inside a perfectly valid 200. The page is public,
	// so the promise ("never rendered back into the page") has to hold on both branches.
	chatServe(t, jsonHandler(http.StatusOK, chatBody("je stuurde: Authorization: Bearer "+testToken)))

	answer, err := vlamChat(context.Background(), testToken, "vlam-stub", "herhaal je context")

	if err != nil {
		t.Fatalf("unexpected failure: %v", err)
	}
	if strings.Contains(answer, testToken) {
		t.Errorf("the token came back inside the answer: %q", answer)
	}
	if !strings.Contains(answer, "<token>") {
		t.Errorf("the answer should keep its shape with the token replaced: %q", answer)
	}
}

// ---- the handler and the page ---------------------------------------------

// postChat drives the real handler the way the form does and returns the page it wrote.
func postChat(t *testing.T, form url.Values) (int, string) {
	t.Helper()
	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodPost, "/vlam-chat", strings.NewReader(form.Encode()))
	request.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	newMux(newResultCache()).ServeHTTP(recorder, request)
	return recorder.Code, recorder.Body.String()
}

func chatForm(model, question string) url.Values {
	return url.Values{"token": {testToken}, "model": {model}, "question": {question}}
}

func TestChatHandlerShowsTheAnswerAndNeverTheToken(t *testing.T) {
	chatServe(t, jsonHandler(http.StatusOK, chatBody(chatAnswer)))

	code, page := postChat(t, chatForm("vlam-stub", "werkt dit?"))

	if code != http.StatusOK {
		t.Fatalf("expected 200, got %d", code)
	}
	if !strings.Contains(page, chatAnswer) {
		t.Errorf("the answer is not on the page: %q", page)
	}
	if strings.Contains(page, testToken) {
		t.Error("the token came back in the page")
	}
	// The form keeps what was typed, so a second attempt is one click - except the token.
	if !strings.Contains(page, `value="werkt dit?"`) {
		t.Error("the question was not kept in the form")
	}
	if !strings.Contains(page, `type="password" name="token" size="40" autocomplete="off" required>`) {
		t.Error("the token field must come back empty, with no value attribute")
	}
}

func TestChatHandlerShowsTheFailureAndNeverTheToken(t *testing.T) {
	chatServe(t, jsonHandler(http.StatusUnauthorized, `{"error":"invalid api key"}`))

	code, page := postChat(t, chatForm("vlam-stub", "werkt dit?"))

	if code != http.StatusOK {
		t.Fatalf("expected the page back with the failure on it, got %d", code)
	}
	if !strings.Contains(page, "Mislukt") || !strings.Contains(page, "the token") {
		t.Errorf("expected the refusal, pointed at the token, on the page: %q", page)
	}
	if strings.Contains(page, testToken) {
		t.Error("the token came back in the page")
	}
}

func TestChatHandlerLogsNeitherTheTokenNorTheConversation(t *testing.T) {
	// The whole log of one round trip, captured: a status page is public and this pod's
	// log is readable by anyone with kubectl, so all three have to be absent from it.
	chatServe(t, jsonHandler(http.StatusOK, chatBody(chatAnswer)))

	logged := captureStdout(t, func() { postChat(t, chatForm("vlam-stub", "werkt dit?")) })

	for what, value := range map[string]string{
		"the token":    testToken,
		"the question": "werkt dit?",
		"the answer":   chatAnswer,
	} {
		if strings.Contains(logged, value) {
			t.Errorf("%s ended up in the log: %q", what, logged)
		}
	}
	if !strings.Contains(logged, "status 200") {
		t.Errorf("expected the status to be logged, got %q", logged)
	}
}

func TestChatHandlerRefusesTheEmptyFields(t *testing.T) {
	seen := chatServe(t, jsonHandler(http.StatusOK, chatBody(chatAnswer)))

	for _, form := range []url.Values{
		{"model": {"vlam-stub"}, "question": {"werkt dit?"}},
		{"token": {testToken}, "question": {"werkt dit?"}},
		{"token": {testToken}, "model": {"vlam-stub"}},
	} {
		_, page := postChat(t, form)
		if !strings.Contains(page, "vul een") {
			t.Errorf("form %v: expected a validation message, got %q", form, page)
		}
	}
	if seen.count != 0 {
		t.Errorf("an incomplete form must not reach VLAM, but it was called %d times", seen.count)
	}
}

func TestChatHandlerIsPostOnly(t *testing.T) {
	t.Setenv("VLAM_API_URL", "http://vlam.invalid")
	recorder := httptest.NewRecorder()
	newMux(newResultCache()).ServeHTTP(recorder, httptest.NewRequest(http.MethodGet, "/vlam-chat", nil))

	if recorder.Code != http.StatusMethodNotAllowed {
		t.Errorf("expected 405 on GET, got %d", recorder.Code)
	}
}

func TestChatHandlerIsAbsentWhenTheServiceIsNotBound(t *testing.T) {
	// No binding, no button, no endpoint: a deployment without vlam has nothing here to
	// post a token to.
	t.Setenv("VLAM_API_URL", "")

	code, _ := postChat(t, chatForm("vlam-stub", "werkt dit?"))

	if code != http.StatusNotFound {
		t.Errorf("expected 404 without the service bound, got %d", code)
	}
}

func TestThePageCarriesTheFormOnlyWhenTheServiceIsBound(t *testing.T) {
	t.Setenv("VLAM_API_URL", "")
	if strings.Contains(renderHTML(newResultCache(), nil, nil), "Test VLAM") {
		t.Error("the chat form must not appear when the vlam service is not bound")
	}

	t.Setenv("VLAM_API_URL", "http://vlam.invalid")
	page := renderHTML(newResultCache(), nil, nil)
	if !strings.Contains(page, "Test VLAM") {
		t.Error("the chat form must appear when the vlam service is bound")
	}
	if !strings.Contains(page, "http://vlam.invalid"+vlamChatPath) {
		t.Errorf("the page should name the fixed destination, got %q", page)
	}
	if !strings.Contains(page, `value="`+defaultChatQuestion+`"`) {
		t.Error("expected the default question to be pre-filled")
	}
}

func TestTheModelFieldIsPrefilledFromTheLastProbeRound(t *testing.T) {
	// One click for the common case: the probe already learned a model name that works.
	t.Setenv("VLAM_API_URL", "http://vlam.invalid")
	cache := newResultCache()
	cache.set(Result{ID: vlamProbeTargetID, Kind: "vlam", Bound: true, OK: boolp(true),
		Detail: map[string]any{"first_model": "vlam-stub"}})

	if !strings.Contains(renderHTML(cache, nil, nil), `value="vlam-stub"`) {
		t.Error("expected the model from the last probe round to be pre-filled")
	}
}

// captureStdout collects everything written to os.Stdout while fn runs.
func captureStdout(t *testing.T, fn func()) string {
	t.Helper()
	reader, writer, err := os.Pipe()
	if err != nil {
		t.Fatalf("pipe: %v", err)
	}
	original := os.Stdout
	os.Stdout = writer
	fn()
	os.Stdout = original
	_ = writer.Close()
	out, _ := io.ReadAll(reader)
	_ = reader.Close()
	return string(out)
}
