# SOPS Skip-If-Unchanged Re-encryption

## What it is

An optimization in the Operations Manager's SOPS encryption step that **keeps
the existing ciphertext when a secret's plaintext has not changed**, instead of
re-encrypting it on every deployment.

SOPS encryption is non-deterministic: every run generates fresh nonces, a new
MAC, and a new `lastmodified` timestamp. So re-encrypting an unchanged secret
produces a byte-different `*.sops.yaml` file every time. Before this change,
every deployment rewrote every generated secret, which churned the GitOps repos
(`zad-deployments`, ArgoCD repository secrets) with meaningless diffs and grew
the commit history with noise on each push.

With this change, a secret whose decrypted content is identical to what's
already committed is left untouched — no re-encryption, no git diff.

## How it works

`encrypt_to_sops_files()` in `opi/utils/sops.py` now takes an optional
`private_key`. When the key is provided, for each `*.to-sops.yaml` it:

1. Locates the existing `*.sops.yaml` output (if any).
2. Decrypts it with the private key.
3. Parses **both** the existing decrypted content and the freshly generated
   plaintext as YAML and compares the parsed documents.
4. If they are equal, it keeps the existing ciphertext verbatim, deletes only
   the plaintext `*.to-sops.yaml` source (so nothing leaks), and skips
   encryption. Otherwise it re-encrypts as before.

The comparison is on **parsed YAML**, not raw bytes, so key-order or formatting
differences in the generated plaintext never count as a change.

It **fails safe — re-encrypting — on any doubt**:

- no existing `*.sops.yaml` (first deployment),
- a decrypt failure (e.g. after AGE key rotation, or a wrong key),
- unparseable YAML on either side,
- no `private_key` passed at all (the original always-re-encrypt behaviour).

So a missing or mismatched key can never cause a stale secret to be kept — at
worst it re-encrypts unnecessarily, exactly as before.

## Configuration

There is nothing to configure. The behaviour is enabled automatically wherever a
private key is available:

- **Project secrets** (deployment secrets, helm values, helmfile values,
  infrastructure secrets) use the project's own AGE key, resolved via
  `ProjectManager._sops_private_key_for()`, which returns `None` for legacy
  projects without an `age-private-key` (those fall back to always
  re-encrypting).
- **ArgoCD repository secrets** use the cluster's `SOPS_AGE_PRIVATE_KEY` from
  settings.

`encrypt_to_sops_files_or_fail()` (the fail-closed wrapper that all managers
use) threads the `private_key` straight through.

## Examples

```python
# Skip-if-unchanged enabled (project key resolved per project):
encrypt_to_sops_files_or_fail(
    target_path,
    public_key,
    f"secrets voor deployment '{deployment_name}' (namespace '{prefixed_namespace}')",
    private_key=await self._sops_private_key_for(project_data),
)

# Always re-encrypt (no key) — original behaviour:
encrypt_to_sops_files(directory, public_key)
```

## Dependencies

- `sops` and `age` binaries (already required by the Operations Manager image).
- `get_decoded_project_private_key()` in `opi/utils/age.py` for the project key.
- `settings.SOPS_AGE_PRIVATE_KEY` for ArgoCD repository secrets.

## Tests

- `tests/test_sops_skip_unchanged.py` — real round-trip tests using the actual
  `sops`/`age` binaries with a throwaway keypair: byte-identical ciphertext on
  unchanged input, re-encryption on change, semantics-vs-formatting, no-key and
  wrong-key fall back to re-encrypt, and first-time encryption. Skipped when the
  binaries are absent.
- `tests/test_sops_fail_abort.py` — includes a guard test asserting the managers
  never call the bare `encrypt_to_sops_files` (the fail-closed wrapper must
  always be used).
