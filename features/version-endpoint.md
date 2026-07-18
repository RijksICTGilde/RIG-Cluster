# Version endpoint & display

ZAD/OPI reports which build is running via a public `GET /version` endpoint and in the page
footer. This makes it possible to confirm exactly what code a running instance is serving -
useful in the sandbox, where skaffold hot-syncs source and it is otherwise hard to tell.

## What you get

- **`GET /version`** (public, unauthenticated like `/health`) returns JSON:
  ```json
  {
    "name": "ZAD",
    "version": "25c6aa29",
    "commit": "25c6aa29b9d118b5afcd19305ad7eae199c0ce21",
    "branch": "main",
    "build_date": "2026-07-17T18:36:43Z",
    "dirty": true
  }
  ```
- **Footer**: renders `ZAD <version> @ <branch> [*] (<build_date>)` and links to the GitHub commit
  (`*` = the working tree was dirty when the version was generated). Location can be moved later.

## How the value is resolved

`opi/core/version.py::get_version_info()` reads, highest priority first (not cached, so live
updates are picked up without a restart):

1. **`opi/version.json`** - generated from git by `task version:generate`. In skaffold dev this
   file is hot-synced into the pod, so `/version` tracks the running commit/branch/dirty state.
2. **Environment variables** baked at image build: `ZAD_VERSION`, `ZAD_GIT_COMMIT`,
   `ZAD_GIT_BRANCH`, `ZAD_BUILD_DATE` (see the `ARG`/`ENV` block in `operations-manager/Dockerfile`).
   Used for CI/prod immutable images.
3. Static defaults (`version: "0.1.0"`).

## Generating the version file

```bash
task version:generate     # write opi/version.json once from git
task version:watch        # regenerate every 5s (for skaffold hot-sync during dev)
```

`task sandbox:skaffold-dev` runs `version:generate` before starting skaffold, so the initial image
is built with the current commit. For the reported commit/dirty state to track edits made *during*
a dev session, run `task version:watch` in a second terminal - skaffold syncs the regenerated
`opi/version.json` into the pod on each change.

`opi/version.json` is git-ignored (generated artifact).

## CI / production

Pass the build's identity as Docker build-args, mirroring Wies (image tag == reported version):

```
docker build \
  --build-arg ZAD_VERSION=<immutable-tag-or-semver> \
  --build-arg ZAD_GIT_COMMIT=<sha> \
  --build-arg ZAD_GIT_BRANCH=<branch> \
  --build-arg ZAD_BUILD_DATE=<iso8601> \
  ...
```

Alternatively, run `task version:generate` in CI before the build so `opi/version.json` is copied
into the image (it takes priority over the env vars).

## Used by the E2E suite

`tests/e2e/test_sandbox_flows.py::test_version_endpoint` asserts `/version` returns 200 with the
expected fields and logs the running build, so every sandbox run records which code it exercised.
