package main

import (
	"context"
	"fmt"
	"sync"
	"time"
)

// checkTimeout bounds each individual resource check so one slow/broken service
// cannot blow the overall verification budget. Checks run concurrently, so the
// total wall-clock stays near a single check even with several bound services.
const checkTimeout = 5 * time.Second

// checkResult is what a probe-kind handler returns.
type checkResult struct {
	ok     bool
	detail map[string]any
	err    error
}

// runAllChecks probes every target concurrently, caches the results, and prints
// the summary banner. Safe to call repeatedly (the runner keeps /status fresh).
func runAllChecks(spec *Spec, cache *resultCache) {
	var wg sync.WaitGroup
	for _, t := range spec.Targets {
		t := t
		wg.Add(1)
		go func() {
			defer wg.Done()
			cache.set(runTarget(t))
		}()
	}
	wg.Wait()
	results, _, _ := cache.snapshot()
	logBanner(results)
	cache.finishRound()
}

// runTarget decides boundness, asserts every injected var is present, then runs
// the kind handler.
func runTarget(t Target) Result {
	r := Result{ID: t.ID, Kind: t.Kind, Services: t.Services}

	if !anyPresent(t.BoundWhenAny) {
		r.Bound = false
		logSkip(t.ID)
		return r
	}
	r.Bound = true

	// Coverage: every var the platform injects for a bound target must be present.
	// A dropped/renamed var is itself a provisioning bug.
	if missing := missingVars(t.EnvVars); len(missing) > 0 {
		r.OK = boolp(false)
		r.MissingVars = missing
		logFail(t.ID, "env-presence", fmt.Errorf("missing injected vars: %v", missing))
		return r
	}

	logStart(t.ID, firstEnv(t.BoundWhenAny...))
	ctx, cancel := context.WithTimeout(context.Background(), checkTimeout)
	defer cancel()
	start := time.Now()

	var cr checkResult
	switch t.Kind {
	case "sql":
		cr = checkSQL(ctx)
	case "redis":
		cr = checkRedis(ctx)
	case "s3":
		cr = checkS3(ctx)
	case "oidc":
		cr = checkOIDC(ctx)
	case "path":
		cr = checkPath(t)
	case "metadata":
		cr = checkMetadata(t)
	case "vlam":
		cr = checkVlam(ctx)
	default:
		cr = checkResult{err: fmt.Errorf("unknown probe kind %q", t.Kind)}
	}

	elapsed := time.Since(start)
	r.LatencyMS = elapsed.Milliseconds()
	r.Detail = cr.detail
	switch {
	case cr.err != nil:
		r.OK = boolp(false)
		r.Error = cr.err.Error()
		logFail(t.ID, "verify", cr.err)
	case cr.ok:
		r.OK = boolp(true)
		logOK(t.ID, "verify", elapsed)
	default:
		r.OK = boolp(false)
		r.Error = "check reported not ok"
		logFail(t.ID, "verify", fmt.Errorf("check reported not ok"))
	}
	return r
}
