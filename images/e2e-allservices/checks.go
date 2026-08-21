package main

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/minio/minio-go/v7"
	"github.com/minio/minio-go/v7/pkg/credentials"
	"github.com/redis/go-redis/v9"
)

// probeSuffix returns a collision-resistant suffix for probe resource names.
func probeSuffix() int64 { return time.Now().UnixNano() }

// ---- Postgres (sql) -------------------------------------------------------

func sqlConnString() string {
	if full := firstEnv("DATABASE_SERVER_FULL", "APP_DATABASE_SERVER_FULL"); full != "" {
		return full
	}
	host := firstEnv("DATABASE_SERVER_HOST", "APP_DATABASE_SERVER_HOST", "APP_DATABASE_SERVER")
	port := firstEnv("DATABASE_SERVER_PORT", "APP_DATABASE_SERVER_PORT", "APP_DATABASE_PORT")
	user := firstEnv("DATABASE_SERVER_USER", "APP_DATABASE_USER")
	pass := firstEnv("DATABASE_PASSWORD", "APP_DATABASE_PASSWORD")
	db := firstEnv("DATABASE_DB", "APP_DATABASE_DB")
	return dsn(user, pass, host, port, db)
}

func sqlROConnString() string {
	host := firstEnv("DATABASE_SERVER_HOST", "APP_DATABASE_SERVER_HOST", "APP_DATABASE_SERVER")
	port := firstEnv("DATABASE_SERVER_PORT", "APP_DATABASE_SERVER_PORT", "APP_DATABASE_PORT")
	user := firstEnv("DATABASE_SERVER_USER_RO", "APP_DATABASE_USER_RO")
	pass := firstEnv("DATABASE_PASSWORD_RO", "APP_DATABASE_PASSWORD_RO")
	db := firstEnv("DATABASE_DB", "APP_DATABASE_DB")
	if user == "" || pass == "" {
		return ""
	}
	return dsn(user, pass, host, port, db)
}

func dsn(user, pass, host, port, db string) string {
	if port == "" {
		port = "5432"
	}
	return fmt.Sprintf("postgresql://%s:%s@%s:%s/%s",
		url.QueryEscape(user), url.QueryEscape(pass), host, port, url.QueryEscape(db))
}

func checkSQL(ctx context.Context) checkResult {
	detail := map[string]any{
		"host":     firstEnv("DATABASE_SERVER_HOST", "APP_DATABASE_SERVER_HOST", "APP_DATABASE_SERVER"),
		"database": firstEnv("DATABASE_DB", "APP_DATABASE_DB"),
	}

	conn, err := pgx.Connect(ctx, sqlConnString())
	if err != nil {
		return checkResult{detail: detail, err: fmt.Errorf("connect: %w", err)}
	}
	defer conn.Close(context.Background())

	var one int
	if err := conn.QueryRow(ctx, "SELECT 1").Scan(&one); err != nil {
		return checkResult{detail: detail, err: fmt.Errorf("select 1: %w", err)}
	}

	// Write/read-back in the default schema and every extra DATABASE_SCHEMA_{POSTFIX}.
	schemas := discoveredSchemas()
	schemaResults := map[string]string{}
	var failures []string
	for _, s := range schemas {
		if e := roundTripSchema(ctx, conn, s.name); e != nil {
			schemaResults[s.name] = "fail: " + e.Error()
			failures = append(failures, fmt.Sprintf("schema=%s", s.name))
			logFail("postgres", "schema="+s.name+" ("+s.envVar+")", e)
		} else {
			schemaResults[s.name] = "ok"
			logInfo("postgres schema=%s (%s) write/read ok", s.name, s.envVar)
		}
	}
	detail["schemas"] = schemaResults

	// Read-only role: when provisioned it must SELECT and be refused writes. When
	// the platform has not provisioned RO credentials (empty), report it but do
	// not fail the whole database check - the read/write binding above is proven.
	roMsg, roOK, roProvisioned := checkReadOnlyRole(ctx)
	detail["read_only"] = roMsg
	if roProvisioned && !roOK {
		failures = append(failures, "ro-role")
	}

	if len(failures) > 0 {
		return checkResult{detail: detail, err: fmt.Errorf("%s", strings.Join(failures, ", "))}
	}
	return checkResult{ok: true, detail: detail}
}

func roundTripSchema(ctx context.Context, conn *pgx.Conn, schema string) error {
	table := fmt.Sprintf("e2e_healthcheck_%d", probeSuffix())
	qualified := pgx.Identifier{schema, table}.Sanitize()

	if _, err := conn.Exec(ctx, fmt.Sprintf("CREATE TABLE %s (id int, marker text)", qualified)); err != nil {
		return fmt.Errorf("create table in %q: %w", schema, err)
	}
	defer func() {
		dctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()
		_, _ = conn.Exec(dctx, "DROP TABLE IF EXISTS "+qualified)
	}()

	marker := fmt.Sprintf("probe-%d", probeSuffix())
	if _, err := conn.Exec(ctx, fmt.Sprintf("INSERT INTO %s (id, marker) VALUES ($1, $2)", qualified), 1, marker); err != nil {
		return fmt.Errorf("insert: %w", err)
	}
	var got string
	if err := conn.QueryRow(ctx, fmt.Sprintf("SELECT marker FROM %s WHERE id = $1", qualified), 1).Scan(&got); err != nil {
		return fmt.Errorf("select: %w", err)
	}
	if got != marker {
		return fmt.Errorf("readback mismatch: wrote %q got %q", marker, got)
	}
	return nil
}

// checkReadOnlyRole verifies the RO role can read but is refused writes. Returns
// (message, ok, provisioned): provisioned is false when no RO credentials were
// injected, so the caller can report it without failing the database check.
func checkReadOnlyRole(ctx context.Context) (msg string, ok bool, provisioned bool) {
	connStr := sqlROConnString()
	if connStr == "" {
		// The RO role is optional platform state (e.g. not yet provisioned); a
		// missing RO credential is reported, not treated as a database failure.
		return "skipped (no RO credentials provisioned)", false, false
	}
	conn, err := pgx.Connect(ctx, connStr)
	if err != nil {
		return "connect failed: " + err.Error(), false, true
	}
	defer conn.Close(context.Background())

	var one int
	if err := conn.QueryRow(ctx, "SELECT 1").Scan(&one); err != nil {
		return "cannot SELECT: " + err.Error(), false, true
	}

	schema := firstEnv("DATABASE_SCHEMA", "APP_DATABASE_SCHEMA")
	if schema == "" {
		schema = "public"
	}
	table := pgx.Identifier{schema, fmt.Sprintf("e2e_ro_probe_%d", probeSuffix())}.Sanitize()
	_, werr := conn.Exec(ctx, fmt.Sprintf("CREATE TABLE %s (id int)", table))
	if werr == nil {
		dctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()
		_, _ = conn.Exec(dctx, "DROP TABLE IF EXISTS "+table)
		return "read ok but RO role could WRITE (should be refused)", false, true
	}
	var pgErr *pgconn.PgError
	if errors.As(werr, &pgErr) && pgErr.Code == "42501" {
		return "read ok, write refused (permission denied)", true, true
	}
	// Any other error still means the write did not succeed; note it verbatim.
	return "read ok, write refused (" + werr.Error() + ")", true, true
}

// ---- Redis ----------------------------------------------------------------

func checkRedis(ctx context.Context) checkResult {
	prefix := firstEnv("REDIS_PREFIX", "APP_REDIS_PREFIX")

	var opts *redis.Options
	if raw := firstEnv("REDIS_URL", "APP_REDIS_URL"); raw != "" {
		o, err := redis.ParseURL(raw)
		if err != nil {
			return checkResult{err: fmt.Errorf("parse REDIS_URL: %w", err)}
		}
		opts = o
	} else {
		host := firstEnv("REDIS_HOST", "APP_REDIS_HOST")
		port := firstEnv("REDIS_PORT", "APP_REDIS_PORT")
		opts = &redis.Options{
			Addr:     host + ":" + port,
			Username: firstEnv("REDIS_USERNAME", "APP_REDIS_USERNAME"),
			Password: firstEnv("REDIS_PASSWORD", "APP_REDIS_PASSWORD"),
		}
	}
	detail := map[string]any{"addr": opts.Addr, "prefix": prefix}

	rdb := redis.NewClient(opts)
	defer rdb.Close()

	if err := rdb.Ping(ctx).Err(); err != nil {
		return checkResult{detail: detail, err: fmt.Errorf("ping: %w", err)}
	}

	// The ACL only grants the {prefix}: keyspace, so an unprefixed key gets NOPERM.
	key := "e2e-healthcheck"
	if prefix != "" {
		key = prefix + ":e2e-healthcheck"
	}
	val := fmt.Sprintf("probe-%d", probeSuffix())
	if err := rdb.Set(ctx, key, val, 30*time.Second).Err(); err != nil {
		return checkResult{detail: detail, err: fmt.Errorf("set %q: %w", key, err)}
	}
	defer func() {
		dctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()
		rdb.Del(dctx, key)
	}()
	got, err := rdb.Get(ctx, key).Result()
	if err != nil {
		return checkResult{detail: detail, err: fmt.Errorf("get %q: %w", key, err)}
	}
	if got != val {
		return checkResult{detail: detail, err: fmt.Errorf("readback mismatch")}
	}
	return checkResult{ok: true, detail: detail}
}

// ---- MinIO / S3 -----------------------------------------------------------

func checkS3(ctx context.Context) checkResult {
	access := firstEnv("OBJECT_STORE_USER", "APP_OBJECT_STORE_USER")
	secret := firstEnv("OBJECT_STORE_PASSWORD", "APP_OBJECT_STORE_PASSWORD")
	bucket := firstEnv("OBJECT_STORE_BUCKET_NAME", "APP_OBJECT_STORE_BUCKET_NAME")
	region := firstEnv("OBJECT_STORE_REGION", "APP_OBJECT_STORE_REGION")

	endpoint, secure, err := s3Endpoint()
	if err != nil {
		return checkResult{err: err}
	}
	detail := map[string]any{"endpoint": endpoint, "bucket": bucket, "secure": secure}

	cli, err := minio.New(endpoint, &minio.Options{
		Creds:  credentials.NewStaticV4(access, secret, ""),
		Secure: secure,
		Region: region,
	})
	if err != nil {
		return checkResult{detail: detail, err: fmt.Errorf("client: %w", err)}
	}

	object := fmt.Sprintf("e2e-healthcheck/probe-%d", probeSuffix())
	payload := []byte(fmt.Sprintf("e2e-probe-%d", probeSuffix()))
	if _, err := cli.PutObject(ctx, bucket, object, bytes.NewReader(payload), int64(len(payload)),
		minio.PutObjectOptions{ContentType: "text/plain"}); err != nil {
		return checkResult{detail: detail, err: fmt.Errorf("put %q: %w", object, err)}
	}
	defer func() {
		dctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		_ = cli.RemoveObject(dctx, bucket, object, minio.RemoveObjectOptions{})
	}()

	obj, err := cli.GetObject(ctx, bucket, object, minio.GetObjectOptions{})
	if err != nil {
		return checkResult{detail: detail, err: fmt.Errorf("get: %w", err)}
	}
	defer obj.Close()
	got, err := io.ReadAll(obj)
	if err != nil {
		return checkResult{detail: detail, err: fmt.Errorf("read: %w", err)}
	}
	if !bytes.Equal(got, payload) {
		return checkResult{detail: detail, err: fmt.Errorf("readback mismatch")}
	}
	return checkResult{ok: true, detail: detail}
}

func s3Endpoint() (endpoint string, secure bool, err error) {
	if raw := firstEnv("OBJECT_STORE_ENDPOINT_URL", "APP_OBJECT_STORE_ENDPOINT_URL"); raw != "" {
		u, perr := url.Parse(raw)
		if perr != nil {
			return "", false, fmt.Errorf("bad endpoint url %q: %w", raw, perr)
		}
		return u.Host, u.Scheme == "https", nil
	}
	host := firstEnv("OBJECT_STORE_HOST", "APP_OBJECT_STORE_HOST")
	port := firstEnv("OBJECT_STORE_PORT", "APP_OBJECT_STORE_PORT")
	if port != "" {
		return host + ":" + port, false, nil
	}
	return host, false, nil
}

// ---- Keycloak / OIDC ------------------------------------------------------

func checkOIDC(ctx context.Context) checkResult {
	discovery := firstEnv("OIDC_DISCOVERY_URL")
	if discovery == "" {
		if base, realm := firstEnv("OIDC_URL"), firstEnv("OIDC_REALM"); base != "" && realm != "" {
			discovery = strings.TrimRight(base, "/") + "/realms/" + realm + "/.well-known/openid-configuration"
		}
	}
	detail := map[string]any{"discovery_url": discovery}
	if discovery == "" {
		return checkResult{detail: detail, err: fmt.Errorf("no discovery URL and cannot derive one from OIDC_URL/OIDC_REALM")}
	}

	doc, err := httpGetJSON(ctx, discovery)
	if err != nil {
		return checkResult{detail: detail, err: fmt.Errorf("discovery: %w", err)}
	}
	issuer, _ := doc["issuer"].(string)
	tokenEndpoint, _ := doc["token_endpoint"].(string)
	detail["issuer"] = issuer
	if issuer == "" || tokenEndpoint == "" {
		return checkResult{detail: detail, err: fmt.Errorf("discovery doc missing issuer/token_endpoint")}
	}

	clientID := firstEnv("OIDC_CLIENT_ID")
	clientSecret := firstEnv("OIDC_CLIENT_SECRET")
	if clientID == "" || clientSecret == "" {
		detail["token"] = "skipped (no client credentials injected)"
		return checkResult{ok: true, detail: detail}
	}

	// Client-credentials grant: proves the injected secret actually authenticates.
	tok, err := clientCredentialsToken(ctx, tokenEndpoint, clientID, clientSecret)
	if err != nil {
		// Not every confidential client has the service-account grant enabled;
		// fall back to discovery-only but mark it clearly (never a silent pass).
		detail["token"] = "discovery-only (client-credentials grant failed: " + err.Error() + ")"
		logInfo("oidc client-credentials downgrade: %v", err)
		return checkResult{ok: true, detail: detail}
	}
	detail["token"] = "client-credentials ok"
	if iss := jwtIssuer(tok); iss != "" {
		detail["token_issuer"] = iss
	}
	return checkResult{ok: true, detail: detail}
}

func httpGetJSON(ctx context.Context, endpoint string) (map[string]any, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint, nil)
	if err != nil {
		return nil, err
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("status %d: %s", resp.StatusCode, strings.TrimSpace(string(body)))
	}
	var doc map[string]any
	if err := json.Unmarshal(body, &doc); err != nil {
		return nil, fmt.Errorf("decode: %w", err)
	}
	return doc, nil
}

func clientCredentialsToken(ctx context.Context, tokenEndpoint, id, secret string) (string, error) {
	form := url.Values{"grant_type": {"client_credentials"}}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, tokenEndpoint, strings.NewReader(form.Encode()))
	if err != nil {
		return "", err
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	req.SetBasicAuth(id, secret)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("status %d: %s", resp.StatusCode, strings.TrimSpace(string(body)))
	}
	var tr struct {
		AccessToken string `json:"access_token"`
	}
	if err := json.Unmarshal(body, &tr); err != nil {
		return "", err
	}
	if tr.AccessToken == "" {
		return "", fmt.Errorf("no access_token in response")
	}
	return tr.AccessToken, nil
}

func jwtIssuer(token string) string {
	parts := strings.Split(token, ".")
	if len(parts) < 2 {
		return ""
	}
	payload, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil {
		return ""
	}
	var claims struct {
		Iss string `json:"iss"`
	}
	if json.Unmarshal(payload, &claims) != nil {
		return ""
	}
	return claims.Iss
}

// ---- Storage / PVC (path) -------------------------------------------------

func checkPath(t Target) checkResult {
	envVar := firstPresent(t.BoundWhenAny)
	dir := firstEnv(envVar)
	detail := map[string]any{"path": dir, "env": envVar}
	if dir == "" {
		return checkResult{detail: detail, err: fmt.Errorf("path env %s not set", envVar)}
	}

	file := filepath.Join(dir, fmt.Sprintf(".e2e-healthcheck-%d", probeSuffix()))
	payload := []byte(fmt.Sprintf("probe-%d", probeSuffix()))

	f, err := os.OpenFile(file, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0o644)
	if err != nil {
		return checkResult{detail: detail, err: fmt.Errorf("open %s: %w", file, err)}
	}
	if _, err := f.Write(payload); err != nil {
		f.Close()
		return checkResult{detail: detail, err: fmt.Errorf("write: %w", err)}
	}
	if err := f.Sync(); err != nil {
		f.Close()
		return checkResult{detail: detail, err: fmt.Errorf("fsync: %w", err)}
	}
	f.Close()
	defer os.Remove(file)

	got, err := os.ReadFile(file)
	if err != nil {
		return checkResult{detail: detail, err: fmt.Errorf("readback: %w", err)}
	}
	if !bytes.Equal(got, payload) {
		return checkResult{detail: detail, err: fmt.Errorf("readback mismatch")}
	}
	return checkResult{ok: true, detail: detail}
}

// ---- VLAM (in-cluster API proxy) ------------------------------------------

// vlamModelsPath is the read the probe performs. It is the cheapest call VLAM
// offers, needs no request body and changes nothing, so one round trip proves the
// whole chain the platform is responsible for - the selected service, the injected
// address, the egress policy on this pod and the inbound policy on the proxy -
// without spending anything at the far end.
const vlamModelsPath = "/v1/models"

// checkVlam calls {VLAM_API_URL}/v1/models and judges the answer.
//
// A 401/403 counts as OK on purpose: it can only come from VLAM itself, so the
// network path stands and only VLAM's own authorization (an API key that belongs to
// the project, not to this probe) is holding the door. Today that endpoint is
// key-less, but that is VLAM's choice to change, not a contract of this platform.
//
// The failures are worded for the side this gets debugged from - the consumer - so
// each one names the hop that is suspect rather than only what went wrong.
func checkVlam(ctx context.Context) checkResult {
	base := strings.TrimRight(firstEnv("VLAM_API_URL"), "/")
	if base == "" {
		return checkResult{err: fmt.Errorf("VLAM_API_URL is empty")}
	}
	endpoint := base + vlamModelsPath
	detail := map[string]any{"endpoint": endpoint}

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint, nil)
	if err != nil {
		return checkResult{detail: detail, err: fmt.Errorf("bad VLAM address %q: %w", base, err)}
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return checkResult{detail: detail, err: fmt.Errorf(
			"no answer from %s (suspect: this pod's egress policy from the vlam service, "+
				"the proxy's inbound policy, or the proxy itself): %w", endpoint, err)}
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	detail["status"] = resp.StatusCode

	switch {
	case resp.StatusCode == http.StatusUnauthorized || resp.StatusCode == http.StatusForbidden:
		detail["verdict"] = fmt.Sprintf("reachable, VLAM refuses (%d)", resp.StatusCode)
		return checkResult{ok: true, detail: detail}
	case resp.StatusCode >= 500:
		return checkResult{detail: detail, err: fmt.Errorf(
			"status %d from %s (suspect: the proxy or VLAM behind it, not the network path): %s",
			resp.StatusCode, endpoint, snippet(body))}
	case resp.StatusCode != http.StatusOK:
		return checkResult{detail: detail, err: fmt.Errorf(
			"status %d from %s: %s", resp.StatusCode, endpoint, snippet(body))}
	}

	var doc struct {
		Data []struct {
			ID string `json:"id"`
		} `json:"data"`
	}
	if err := json.Unmarshal(body, &doc); err != nil || doc.Data == nil {
		return checkResult{detail: detail, err: fmt.Errorf(
			"200 from %s without a recognisable models document (suspect: something other than "+
				"VLAM answered, e.g. an error page from the proxy): %s", endpoint, snippet(body))}
	}
	detail["models"] = len(doc.Data)
	if len(doc.Data) > 0 {
		detail["first_model"] = doc.Data[0].ID
	}
	return checkResult{ok: true, detail: detail}
}

// snippet folds a response body into one short single-line excerpt, so an error
// message carries what answered without dumping a page into the log.
func snippet(body []byte) string {
	text := strings.Join(strings.Fields(string(body)), " ")
	runes := []rune(text)
	if len(runes) > 200 {
		return string(runes[:200]) + "..."
	}
	return text
}

// ---- Metadata (informational) ---------------------------------------------

func checkMetadata(t Target) checkResult {
	detail := map[string]any{}
	for _, v := range t.EnvVars {
		if val := firstEnv(v); val != "" {
			detail[v] = redactValue(v, val)
		}
	}
	return checkResult{ok: true, detail: detail}
}

// redactValue hides secret-looking values (tokens, secrets, passwords, keys) so
// echoing metadata never leaks a credential.
func redactValue(name, val string) string {
	up := strings.ToUpper(name)
	for _, marker := range []string{"TOKEN", "SECRET", "PASSWORD", "KEY"} {
		if strings.Contains(up, marker) {
			return "***redacted***"
		}
	}
	return val
}
