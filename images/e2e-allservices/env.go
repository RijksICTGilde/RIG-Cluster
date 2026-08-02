package main

import (
	"os"
	"regexp"
	"strings"
)

// firstEnv returns the first non-empty value among names (canonical first, then
// APP_ aliases). Empty string if none is set.
func firstEnv(names ...string) string {
	for _, n := range names {
		if v := strings.TrimSpace(os.Getenv(n)); v != "" {
			return v
		}
	}
	return ""
}

// present reports whether name is set to a non-empty (trimmed) value.
func present(name string) bool {
	return strings.TrimSpace(os.Getenv(name)) != ""
}

// anyPresent reports whether any of the names is present.
func anyPresent(names []string) bool {
	for _, n := range names {
		if present(n) {
			return true
		}
	}
	return false
}

// firstPresent returns the first of names that is set to a non-empty value, or "".
func firstPresent(names []string) string {
	for _, n := range names {
		if present(n) {
			return n
		}
	}
	return ""
}

// missingVars returns the subset of names that are absent or empty.
func missingVars(names []string) []string {
	var missing []string
	for _, n := range names {
		if !present(n) {
			missing = append(missing, n)
		}
	}
	return missing
}

// extraSchemaPattern matches the dynamic per-schema env vars the platform injects
// for RC-17 extra database schemas: DATABASE_SCHEMA_{POSTFIX}. The plain
// DATABASE_SCHEMA and the APP_-prefixed aliases are handled separately.
var extraSchemaPattern = regexp.MustCompile(`^DATABASE_SCHEMA_[A-Z0-9_]+$`)

// discoveredSchemas returns the default schema plus every DATABASE_SCHEMA_{POSTFIX}
// value found in the environment, de-duplicated and each with the env var that
// carried it (for logging). The default schema is first.
type schemaBinding struct {
	name   string // schema name (the value)
	envVar string // the env var it came from
}

func discoveredSchemas() []schemaBinding {
	var out []schemaBinding
	seen := map[string]bool{}

	if def := firstEnv("DATABASE_SCHEMA", "APP_DATABASE_SCHEMA"); def != "" {
		out = append(out, schemaBinding{name: def, envVar: "DATABASE_SCHEMA"})
		seen[def] = true
	}
	for _, kv := range os.Environ() {
		eq := strings.IndexByte(kv, '=')
		if eq < 0 {
			continue
		}
		key, val := kv[:eq], strings.TrimSpace(kv[eq+1:])
		if val == "" || !extraSchemaPattern.MatchString(key) {
			continue
		}
		if seen[val] {
			continue
		}
		out = append(out, schemaBinding{name: val, envVar: key})
		seen[val] = true
	}
	return out
}
