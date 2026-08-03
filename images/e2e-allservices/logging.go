package main

import (
	"fmt"
	"os"
	"sort"
	"time"
)

// Logging is a first-class feature: one clear, timestamped line per step to
// stdout, so `kubectl logs` alone tells the full story. Secret values are never
// logged - only hosts, users, buckets, key names.

func ts() string { return time.Now().UTC().Format("2006-01-02T15:04:05.000Z") }

// logStart records the beginning of a check.
func logStart(target, detail string) {
	fmt.Printf("%s START %-12s %s\n", ts(), target, detail)
}

// logOK records a successful operation with its latency.
func logOK(target, op string, latency time.Duration) {
	fmt.Printf("%s OK    %-12s %s (%dms)\n", ts(), target, op, latency.Milliseconds())
}

// logFail records a failed operation with the underlying error verbatim.
func logFail(target, op string, err error) {
	fmt.Printf("%s FAIL  %-12s %s: %v\n", ts(), target, op, err)
}

// logSkip records a target that is not bound in this deployment.
func logSkip(target string) {
	fmt.Printf("%s SKIP  %-12s (not bound)\n", ts(), target)
}

// logInfo is a free-form informational line.
func logInfo(format string, args ...any) {
	fmt.Printf("%s INFO  %s\n", ts(), fmt.Sprintf(format, args...))
}

// logBanner prints the end-of-round summary so a human sees the verdict at once.
func logBanner(results []Result) {
	bound, ok := 0, 0
	var failed []string
	for _, r := range results {
		if !r.Bound {
			continue
		}
		bound++
		if r.OK != nil && *r.OK {
			ok++
		} else {
			failed = append(failed, r.ID)
		}
	}
	if len(failed) == 0 {
		fmt.Printf("%s ===== ALL OK (%d/%d bound services verified) =====\n", ts(), ok, bound)
		return
	}
	fmt.Fprintf(os.Stderr, "%s ===== FAILED: %v (%d/%d ok) =====\n", ts(), failed, ok, bound)
}

func sortStrings(s []string) { sort.Strings(s) }
