package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

// ---- VLAM chat completion (manual, human-triggered) ------------------------
//
// The periodic vlam probe reads the model catalogue: it proves the chain the platform
// is responsible for, but it deliberately carries no credential, so it says nothing
// about whether a project's own token opens the door and whether a model answers. That
// last stretch needs something only a human has - a token - so it hangs off a button on
// the status page, the same way the testmail button does for the relay.
//
// The token is used for exactly one outgoing request and then dropped: it is never
// stored, never logged and never rendered back into the page. The destination is fixed
// too - the injected VLAM_API_URL and this one path - so the button is not a proxy a
// visitor can point anywhere.

// vlamChatPath is the one path this button posts to. Fixed, not caller-supplied.
const vlamChatPath = "/v1/chat/completions"

// vlamChatTimeout is generous on purpose: a language model thinks before it answers,
// and this is a human waiting on a button rather than a probe inside a check round.
const vlamChatTimeout = 5 * time.Minute

// vlamProbeTargetID is the /status key the probe spec gives the vlam target. Used only
// to pre-fill the model field with what the last probe round saw.
const vlamProbeTargetID = "vlam"

// defaultChatQuestion is a throwaway question: short to answer, and it costs the far
// end as little as a real chat completion can.
const defaultChatQuestion = "Antwoord in een korte zin: werkt deze verbinding?"

// vlamBound reports whether the vlam service is bound to this component.
func vlamBound() bool {
	return present("VLAM_API_URL")
}

// vlamLastModel returns the model id the last probe round saw, or "" when the probe has
// not run yet or VLAM listed no models.
func vlamLastModel(cache *resultCache) string {
	results, _, _ := cache.snapshot()
	for _, r := range results {
		if r.ID != vlamProbeTargetID {
			continue
		}
		if id, ok := r.Detail["first_model"].(string); ok {
			return id
		}
	}
	return ""
}

// chatRequest is the smallest OpenAI-shaped body that asks one question. No streaming
// and no history: this is a proof, not a chat client.
type chatRequest struct {
	Model    string        `json:"model"`
	Messages []chatMessage `json:"messages"`
	Stream   bool          `json:"stream"`
}

type chatMessage struct {
	Role    string `json:"role"`
	Content string `json:"content"`
}

// vlamChat asks `question` of `model` at {VLAM_API_URL}/v1/chat/completions with the
// caller's token, and returns the content of the first choice.
//
// The failures are worded the way the probe's are - for the side this gets debugged
// from - so each one names the hop that is suspect rather than only what went wrong.
// Which hop that is differs from the probe's, because a credential is now in play: no
// answer at all is the network path, a refusal is the token, and a rejected request is
// the model name.
func vlamChat(ctx context.Context, token, model, question string) (string, error) {
	base := strings.TrimRight(firstEnv("VLAM_API_URL"), "/")
	if base == "" {
		return "", fmt.Errorf("VLAM_API_URL is empty: the vlam service is not bound to this component")
	}
	endpoint := base + vlamChatPath

	payload, err := json.Marshal(chatRequest{
		Model:    model,
		Messages: []chatMessage{{Role: "user", Content: question}},
		Stream:   false,
	})
	if err != nil {
		return "", fmt.Errorf("building the request: %w", err)
	}

	ctx, cancel := context.WithTimeout(ctx, vlamChatTimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, bytes.NewReader(payload))
	if err != nil {
		return "", fmt.Errorf("bad VLAM address %q: %w", base, err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+token)

	start := time.Now()
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return "", fmt.Errorf(
			"no answer from %s (suspect: the network path - this pod's egress policy from the vlam "+
				"service, the proxy's inbound policy, or the proxy itself): %w", endpoint, err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	// Only the status and how long it took. Never the token, the question or the answer:
	// this pod's log is read by anyone with kubectl, and none of those three belong there.
	logInfo("vlam chat: status %d (%dms)", resp.StatusCode, time.Since(start).Milliseconds())

	// The far end could quote the request back - in an error body, or in an answer from a
	// model that was asked to repeat its context. Strip the token from EVERYTHING that is
	// about to be rendered, so "never shown again" holds on both branches.
	redact := func(s string) string { return strings.ReplaceAll(s, token, "<token>") }
	excerpt := func() string { return redact(snippet(body)) }

	switch {
	case resp.StatusCode == http.StatusUnauthorized || resp.StatusCode == http.StatusForbidden:
		return "", fmt.Errorf(
			"status %d from %s (suspect: the token - the path stands, VLAM refuses this caller): %s",
			resp.StatusCode, endpoint, excerpt())
	case resp.StatusCode >= 500:
		return "", fmt.Errorf(
			"status %d from %s (suspect: the proxy or VLAM behind it, not the network path): %s",
			resp.StatusCode, endpoint, excerpt())
	case resp.StatusCode == http.StatusNotFound || resp.StatusCode == http.StatusBadRequest ||
		resp.StatusCode == http.StatusUnprocessableEntity:
		return "", fmt.Errorf(
			"status %d from %s (suspect: the model name %q, or this VLAM does not serve chat "+
				"completions): %s", resp.StatusCode, endpoint, model, excerpt())
	case resp.StatusCode != http.StatusOK:
		return "", fmt.Errorf("status %d from %s: %s", resp.StatusCode, endpoint, excerpt())
	}

	var doc struct {
		Choices []struct {
			Message struct {
				Content string `json:"content"`
			} `json:"message"`
		} `json:"choices"`
	}
	if err := json.Unmarshal(body, &doc); err != nil || len(doc.Choices) == 0 {
		return "", fmt.Errorf(
			"200 from %s without a recognisable chat completion (suspect: something other than "+
				"VLAM answered, e.g. an error page from the proxy): %s", endpoint, excerpt())
	}
	return redact(doc.Choices[0].Message.Content), nil
}
