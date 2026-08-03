# Upgrade-safety replay fixtures

Sanitized, structurally faithful copies of production project files, used by
`tests/test_upgrade_safety_replay.py` (the Layer-1 offline replay of RC-19).

These stand in for the real `zad-projects` files when a projects checkout is not
available on the machine (a CI runner). When the real files *are* available, point
the test at them with `RIG_PROJECTS_DIR=/path/to/projects` and it replays those in
addition to these.

Rules for these fixtures:
- No real secrets. Every AGE-encrypted value is the opaque placeholder
  `base64+age:...` — the replay never decrypts, so an opaque string is enough
  (see the plan, section 2a: Layer 1 needs no key).
- Each file is chosen to exercise a distinct migration/validation path a real
  production file would hit: a current v2 file, a legacy top-level `invites:`
  block (relocated to the invite service at v2.6), a broad multi-service project,
  and an old v1 file (`uses-services`).
- They must migrate **and** validate cleanly under the new code. A fixture that
  fails is a real finding, not a broken test — fix the code, not the fixture.
