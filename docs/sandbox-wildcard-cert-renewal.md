# Sandbox wildcard certificate renewal

Runbook for renewing the Let's Encrypt wildcard certificate used by the
`sandboxed-local` cluster type. The cert is valid for 90 days and lives at
`security/tls/sandbox-wildcard/` (AGE-encrypted in git).

## The trick (what makes this work)

The sandbox is a Kind cluster running on `127.0.0.1`, but the URLs it serves use
a real public domain (`*.sandbox.rijksapp.dev`) with a real TLS cert. That works
because:

1. Public DNS for `*.sandbox.rijksapp.dev` (and the apex `sandbox.rijksapp.dev`)
   has A records pointing to `127.0.0.1` — managed in TransIP under the
   `rijksapp.dev` zone.
2. A real wildcard certificate is obtained from Let's Encrypt via the **DNS-01
   challenge**: we set `_acme-challenge.sandbox.rijksapp.dev` TXT to a value
   that Let's Encrypt provides, they verify it via public DNS, and issue the
   cert. No webserver, no port 80, no incoming traffic — just DNS.
3. The cert + key are AGE-encrypted and committed to the repo so any developer
   can decrypt and use them in their local sandbox.

Because the TransIP API key is **IP-whitelisted to the production cluster**,
the TXT record can't be set from a developer laptop. The renewal script works
around this by calling `kubectl exec` into the `operations-manager` pod on the
production cluster — that pod's egress IP is on the whitelist, and its
ServiceAccount has read access to `secret/transip-credentials`.

## When to renew

- **Every 90 days.** Let's Encrypt certs expire after 90 days. Renew at least
  a week early to leave margin.
- Check expiry: `openssl x509 -in security/tls/sandbox-wildcard/fullchain.pem -noout -dates`
- A renewal that runs past expiry breaks every sandbox until a fresh cert is
  committed and pulled.

## Prerequisites

On the renewing developer's machine:

- `certbot` (`brew install certbot`)
- `age` (`brew install age`)
- `dig` (ships with macOS)
- `kubectl` configured against the **production** cluster
  (`api-prd1-gn2-quattro-rijksapps-nl:6443`)
- Read access to `secret/transip-credentials` in `rig-prd-operations` — this is
  not granted by the production OPI ServiceAccount itself; the renewer needs
  their own kubeconfig with rights to `kubectl exec` into `operations-manager`
- `security/developer-key.txt` (AGE private key, same one used to decrypt the
  existing `.age` files — ask the team if you don't have it)

## Run it

```bash
task sandbox:renew-wildcard-cert
# or directly:  scripts/renew-sandbox-cert.sh
```

That's it. The script is idempotent and unattended (no prompts). It:

1. Finds the `operations-manager` pod in `rig-prd-operations`.
2. Copies `transip_set_dns.py` and `transip_delete_dns.py` into the pod's
   `/tmp/`.
3. Runs `certbot certonly --manual` with:
   - `--manual-auth-hook scripts/certbot/auth-hook.sh` — sets the
     `_acme-challenge.sandbox` TXT via TransIP and waits until Google's
     resolver returns the new value.
   - `--manual-cleanup-hook scripts/certbot/cleanup-hook.sh` — deletes that
     TXT after Let's Encrypt validates.
   - `--config-dir`/`--work-dir`/`--logs-dir` pointed at a tempdir so no
     `sudo` is needed and `/etc/letsencrypt` stays untouched.
4. Copies the issued `fullchain.pem` + `privkey.pem` to
   `security/tls/sandbox-wildcard/`.
5. Re-encrypts both to `.age` using the recipient derived from
   `security/developer-key.txt`. The recipient stays the same as the previous
   cert, so all developers who could decrypt before can still decrypt.

Total runtime: ~30 seconds when DNS propagates quickly (typical), up to 6
minutes worst case.

### Overriding the defaults

```bash
DOMAIN='*.sandbox.example.com' \
TRANSIP_ZONE=example.com \
EMAIL=you@example.com \
OPI_NAMESPACE=rig-prd-operations \
  scripts/renew-sandbox-cert.sh
```

`DOMAIN` must be a subdomain of `TRANSIP_ZONE` (the script derives the TXT
record name by stripping the zone suffix).

## After running

```bash
# Verify the new cert
openssl x509 -in security/tls/sandbox-wildcard/fullchain.pem \
  -noout -dates -subject -ext subjectAltName

# Confirm the .age files decrypt cleanly with the developer key
age -d -i security/developer-key.txt \
  security/tls/sandbox-wildcard/fullchain.pem.age | head -1
```

Then commit **only the `.age` files**:

```bash
git add security/tls/sandbox-wildcard/fullchain.pem.age \
        security/tls/sandbox-wildcard/privkey.pem.age
git commit -m "Renew sandbox wildcard certificate (valid until YYYY-MM-DD)"
```

The plaintext `.pem` files are already in `.gitignore`
(`/security/tls/**/*.pem`).

To activate the new cert in your running sandbox:

```bash
task sandbox:import-wildcard-cert
```

Other developers pull the commit and run the same task.

## What the hooks do (under the hood)

`scripts/certbot/auth-hook.sh` (run by certbot, with `CERTBOT_DOMAIN` and
`CERTBOT_VALIDATION` in env):

1. Derives the TXT record name from `CERTBOT_DOMAIN` and `TRANSIP_ZONE`
   (e.g. `sandbox.rijksapp.dev` in zone `rijksapp.dev` → `_acme-challenge.sandbox`).
2. `kubectl exec` into the OPI pod. Inside the pod:
   - Reads `secret/transip-credentials` via the pod's ServiceAccount token.
   - Exports `TRANSIP_ACCOUNT_NAME` and `TRANSIP_PRIVATE_KEY`.
   - Runs `python3 /tmp/transip_set_dns.py --replace` to upsert the TXT.
3. Polls `dig +short TXT _acme-challenge.<domain> @8.8.8.8` until the new
   value appears (max 5 minutes, 5s between polls), then sleeps an extra 5s
   so the other Let's Encrypt resolvers also see it.

`scripts/certbot/cleanup-hook.sh` (run by certbot after success or failure):

1. Same record-name derivation.
2. `kubectl exec` into OPI → `transip_delete_dns.py --yes` to remove the TXT.
3. Errors are downgraded to warnings so a failed cleanup doesn't mask a
   successful issuance.

## Why we run via `kubectl exec` instead of locally

TransIP's API key has an **IP whitelist**. Calls from a developer laptop
return 401. The whitelist contains the egress IPs of the production cluster,
so any pod in that cluster works as a "jump host" for the API call. The OPI
pod is convenient: it already has Python + the `cryptography` library
installed (used by both `transip_set_dns.py` and `transip_delete_dns.py`),
and its ServiceAccount has read on `secret/transip-credentials`.

The credentials never leave the cluster: the hooks fetch the secret via the
in-cluster Kubernetes API using the pod's SA token, never via `kubectl get
secret` on the developer's host.

## Troubleshooting

**`certbot` reports validation failure ("DNS problem: NXDOMAIN looking up TXT")**

The TXT record wasn't visible to Let's Encrypt's resolvers when they checked.
- The auth-hook polls Google (`8.8.8.8`) only. If TransIP is slow to publish
  to other resolvers, raise the grace `sleep` after propagation in
  `scripts/certbot/auth-hook.sh`.
- Re-run the script; nothing is committed yet, and `--replace` will overwrite
  any stale TXT.

**`kubectl exec` fails with `forbidden`**

You don't have `pods/exec` on the `operations-manager` pod in
`rig-prd-operations`. Ask a cluster admin to grant the role.

**`age-keygen -y security/developer-key.txt` outputs a different recipient
than the existing `.age` files**

Wrong developer key. The `.age` recipients must match across renewals so
other developers can still decrypt. If you really need to rotate the
recipient, all developers need the new private key — coordinate explicitly.

**The cert was issued but `task sandbox:import-wildcard-cert` says the file
isn't there**

The plaintext `.pem` files are gitignored, so other developers won't have
them after a `git pull` until they run `task sandbox:decrypt-wildcard-cert`
(which uses their `security/developer-key.txt`).

## Files

| File | Role |
|---|---|
| `scripts/renew-sandbox-cert.sh` | Entry point — runs the full flow. |
| `scripts/certbot/auth-hook.sh` | Sets the `_acme-challenge` TXT via OPI. |
| `scripts/certbot/cleanup-hook.sh` | Deletes the TXT after validation. |
| `operations-manager/python/scripts/transip_set_dns.py` | Idempotent add/replace for a single DNS record (TXT/A/AAAA/CNAME). |
| `operations-manager/python/scripts/transip_delete_dns.py` | Delete one or more records by name/type/content. |
| `security/tls/sandbox-wildcard/fullchain.pem.age` | Committed, encrypted. |
| `security/tls/sandbox-wildcard/privkey.pem.age` | Committed, encrypted. |
| `security/tls/sandbox-wildcard/*.pem` | Plaintext, gitignored. |
