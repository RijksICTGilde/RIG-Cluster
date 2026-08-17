package main

import "sync"

// Result is the outcome of probing one target. Marshals into /status.
type Result struct {
	ID   string `json:"id"`
	Kind string `json:"kind"`
	// Bound is true when the target's connection vars were injected.
	Bound bool `json:"bound"`
	// OK is the verdict: true/false when bound, nil (null) when skipped.
	OK        *bool  `json:"ok"`
	LatencyMS int64  `json:"latency_ms,omitempty"`
	Error     string `json:"error,omitempty"`
	// MissingVars is set when the target is bound but some injected var is absent.
	MissingVars []string `json:"missing_vars,omitempty"`
	// Detail carries kind-specific extras (schemas map, oidc issuer, ...).
	Detail map[string]any `json:"detail,omitempty"`
	// Services that map to this target (traceability).
	Services []string `json:"services,omitempty"`
}

func boolp(b bool) *bool { return &b }

// resultCache holds the latest Result per target id, refreshed by the runner and
// read by the HTTP handlers.
type resultCache struct {
	mu      sync.RWMutex
	byID    map[string]Result
	round   int  // completed refresh rounds
	firstOK bool // a full round has completed at least once
}

func newResultCache() *resultCache {
	return &resultCache{byID: map[string]Result{}}
}

func (c *resultCache) set(r Result) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.byID[r.ID] = r
}

func (c *resultCache) finishRound() {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.round++
	c.firstOK = true
}

// snapshot returns the current results sorted by id, plus whether a full round
// has completed and the overall verdict (all bound targets OK).
func (c *resultCache) snapshot() (results []Result, ready bool, allOK bool) {
	c.mu.RLock()
	defer c.mu.RUnlock()
	ready = c.firstOK
	allOK = true
	ids := make([]string, 0, len(c.byID))
	for id := range c.byID {
		ids = append(ids, id)
	}
	sortStrings(ids)
	for _, id := range ids {
		r := c.byID[id]
		results = append(results, r)
		if r.Bound && (r.OK == nil || !*r.OK) {
			allOK = false
		}
	}
	return results, ready, allOK
}
