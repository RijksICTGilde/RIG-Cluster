# SOPS Decryption Investigation - Post-Mortem

**Date**: 2026-02-02
**Duration**: Full day (~9+ hours)
**Issue**: SOPS encrypted files cannot be decrypted
**Status**: RESOLVED

---

## TL;DR

macOS Sequoia update broke Docker builds. Dockerfile downloaded x86_64 SOPS binary, but container ran on arm64. QEMU emulation silently corrupted encryption output - files looked valid, decrypted fine on the pod, but failed everywhere else. Fix: detect architecture instead of hardcoding `amd64`. Nine hours of debugging. Four lines of code.

---

## Executive Summary

What started as a routine deployment turned into a full-day investigation when SOPS-encrypted secrets suddenly stopped decrypting. The operations manager could encrypt files without errors, but ArgoCD and local tools couldn't decrypt them.

**The culprit?** A silent architecture mismatch caused by QEMU emulation producing cryptographically valid-looking but actually corrupted output. The encrypted files passed all validation, worked within the pod, but were fundamentally broken for any native arm64 system.

**Time to resolution**: ~9 hours
**Red herrings investigated**: Multiple
**Actual lines of code changed**: 4

---

## The Day From Hell

### It Started With Sequoia

The morning began with a macOS Sequoia update. Nothing unusual - just keeping the system current. What we didn't know was that this update would trigger a cascade of subtle changes that would consume our entire day.

The name "Sequoia" would haunt us - appearing not just in the macOS update, but surfacing in various logs and error messages throughout the investigation, always seeming relevant but never being the actual cause.

### Then Came the Disk Space

As if the universe wanted to make debugging harder, we ran into disk space problems mid-investigation. Docker images, build caches, and Kubernetes resources were fighting for space. This added noise to every troubleshooting step - was this error real, or just a disk space artifact?

### The Usual Suspects

We had recently made significant changes to the codebase:

- **Operations Manager refactoring** - New code paths, updated dependencies, migration to `uv`
- **CMP Kustomize SOPS plugin** - Changes to how secrets were processed
- **Python subprocess changes** - `capture_output=True` replacing explicit pipes
- **Added `contextlib`** - New import for temp file cleanup

Every single one of these seemed like a plausible cause. We investigated them all. We reverted changes. We compared diffs. We stared at code until our eyes burned.

**None of them were the issue.**

---

## The Investigation

### What We Tried (The Long List)

1. **Reverted to month-old operations manager version** - Still broken
2. **Checked AGE key pairs** - Keys were valid, public/private matched
3. **Tested local SOPS encryption/decryption** - Worked perfectly
4. **Examined the `capture_output=True` change** - Functionally identical to explicit pipes
5. **Investigated `contextlib` import** - Only used for cleanup, irrelevant
6. **Verified environment variables** - `SOPS_AGE_KEY` was properly set
7. **Compared SOPS versions** - Same version everywhere
8. **Tested pure AGE encryption from pod** - This worked!
9. **Tested SOPS encryption from pod** - Encrypted fine, but...

### The Breakthrough

The critical observation: **Pure AGE encryption from the pod worked cross-platform, but SOPS encryption didn't.**

Both use AGE under the hood. Both ran on the same pod. Why would one work and the other fail?

```bash
# This worked (pod -> local):
kubectl exec ... -- age --encrypt --recipient $PUBKEY <<< "test" > /tmp/test.age
age --decrypt --identity $PRIVKEY /tmp/test.age  # SUCCESS

# This failed (pod -> local):
kubectl exec ... -- sops --encrypt --age $PUBKEY test.yaml > /tmp/test.sops.yaml
SOPS_AGE_KEY=$PRIVKEY sops --decrypt /tmp/test.sops.yaml  # FAILED
```

We checked the binary architectures:

```bash
# AGE binary - installed via apt-get
$ file /usr/bin/age
ELF 64-bit LSB executable, ARM aarch64  # Native!

# SOPS binary - downloaded in Dockerfile
$ od -A x -t x1 -N 20 /usr/local/bin/sops
000010 02 00 3e 00  # 0x3e = x86_64 - WRONG!
```

**The SOPS binary was x86_64 running under QEMU emulation on an arm64 container.**

---

## Root Cause

### The Architecture Mismatch

The Dockerfile had hardcoded amd64 downloads:

```dockerfile
# SOPS - hardcoded amd64
curl -LO ".../sops-${VERSION}.linux.amd64"

# MinIO client - hardcoded amd64
curl -LO "https://dl.min.io/client/mc/release/linux-amd64/mc"

# Kopia - hardcoded x64
curl -LO ".../kopia-${VERSION}-linux-x64.tar.gz"

# Chisel - hardcoded amd64
curl -LO ".../chisel_${VERSION}_linux_amd64.gz"
```

Meanwhile, `kubectl` was already doing it right:
```dockerfile
ARCH=$(dpkg --print-architecture)
curl -LO "https://dl.k8s.io/release/v1.31.4/bin/linux/${ARCH}/kubectl"
```

### Why QEMU Emulation Broke Cryptography

QEMU emulation of x86_64 binaries on arm64 is known to have issues with:
- Floating point edge cases
- SIMD instructions
- **Cryptographic operations**

The emulated SOPS produced output that:
- Passed all format validation
- Could be decrypted by the same emulated SOPS
- **Failed on any native arm64 system**

The encryption was technically "correct" from QEMU's perspective, but the underlying bytes were subtly wrong. Docker's own documentation warns:

> "Attempts to run Intel-based containers on Apple silicon machines under emulation can crash as QEMU sometimes fails to run the container."

We didn't get crashes. We got something worse: **silent data corruption**.

### Why Did This Surface Today?

The macOS Sequoia update likely triggered changes in Docker Desktop (version 4.37.2):
- Default build platform behavior may have changed
- BuildKit cache invalidation forced fresh binary downloads
- QEMU emulation behavior might have been updated

The `docker build` command in our Taskfile had no `--platform` flag, so it defaulted to the host architecture. On Apple Silicon, that means `linux/arm64`.

Previously, either:
- Images were being built as `linux/amd64` (entire container emulated, but internally consistent)
- BuildKit was caching old layers with working binaries
- Some other default was producing amd64 images

We'll never know exactly what changed. What matters is the fix.

---

## The Fix

Four lines of architecture detection:

```dockerfile
# SOPS
ARCH=$(dpkg --print-architecture)
curl -LO ".../sops-${VERSION}.linux.${ARCH}"

# MinIO
ARCH=$(dpkg --print-architecture)
curl -LO "https://dl.min.io/client/mc/release/linux-${ARCH}/mc"

# Kopia (uses different naming)
ARCH=$(dpkg --print-architecture)
if [ "$ARCH" = "amd64" ]; then KOPIA_ARCH="x64"; else KOPIA_ARCH="$ARCH"; fi
curl -LO ".../kopia-${VERSION}-linux-${KOPIA_ARCH}.tar.gz"

# Chisel
ARCH=$(dpkg --print-architecture)
curl -LO ".../chisel_${VERSION}_linux_${ARCH}.gz"
```

### Verification

After rebuilding:

```bash
# Confirm native arm64 SOPS (0xb7 = aarch64)
$ kubectl exec ... -- od -A x -t x1 -N 20 /usr/local/bin/sops
000010 02 00 b7 00  # Was 3e 00 (x86_64), now b7 00 (arm64)

# Cross-platform decryption now works
$ SOPS_AGE_KEY="..." sops --decrypt /tmp/test.sops.yaml
secret_key: this-is-a-test-value-12345  # SUCCESS!
```

---

## Why This Was So Hard to Find

| Symptom | What It Suggested | Reality |
|---------|-------------------|---------|
| Encryption succeeded | Code is working | QEMU produced corrupted output silently |
| Decryption worked on pod | Encryption is valid | Same broken SOPS could read its own broken output |
| Error: "failed to decrypt" | Key mismatch? | Cryptographic corruption from emulation |
| Code reverts didn't help | Not a code issue | Correct, but we didn't know why yet |
| Recent changes to ops-manager | Probably the cause | Complete red herring |
| `capture_output=True` change | Subprocess issue? | Functionally identical, wasted hours |
| Disk space errors | Everything is broken | Noise that made debugging harder |

---

## Lessons Learned

### Technical

1. **Always use architecture detection** in Dockerfiles for binary downloads
2. **Never trust QEMU for cryptographic operations** - subtle corruption is worse than crashes
3. **Consider multi-platform builds** (`--platform linux/amd64,linux/arm64`)
4. **Test cross-platform scenarios** - encryption on system A, decryption on system B
5. **Check binary architectures early** when debugging mysterious failures

### Process

1. **macOS updates can have far-reaching effects** - Docker Desktop behavior can change
2. **Recent code changes are attractive red herrings** - don't assume correlation is causation
3. **When reverts don't help, look at the environment** - the code might not be the problem
4. **Binary-level inspection (ELF headers) can reveal what logs cannot**

### The Irony

The one binary that was already doing architecture detection correctly (`kubectl`) was the hint we needed. It was right there in the Dockerfile, showing the correct pattern. We just didn't look closely enough, soon enough.

---

## Files Changed

- `operations-manager/Dockerfile` - Added architecture detection for SOPS, MinIO, Kopia, Chisel

---

## Environment Details

| Component | Value |
|-----------|-------|
| Kubernetes nodes | arm64 (aarch64) |
| Build machine | Apple Silicon Mac (arm64) |
| macOS | Sequoia (updated same day) |
| Docker Desktop | 4.37.2 |
| SOPS version | 3.11.0 |
| AGE | Native arm64 (apt-get) |

---

*"The bug that worked perfectly - until it didn't."*
