# Service review checklist

Point a session at this to audit one service end to end. `instructions/services.md` says how to
build a service; this says how to prove one is correct.

Every item is a check with a command or a concrete thing to look at, and a statement of what
"good" looks like. Where an item exists because we actually got it wrong, that is written down:
those are the ones that do not announce themselves.

Report per item: pass, fail, or not applicable with the reason. "Looks fine" is not a result.

---

## 0. Measure, never assume

The single most expensive mistake in this codebase is inventorying by reading file names or
grepping for a word. Two service inventories were wrong that way in one afternoon:
`persistent-storage` and `temp-storage` look like they have no config model because their
package has no `config_model.py`, but they share one through `catalog/shared/storage.py`.

Read the truth from the registry and from real project files:

```python
from opi.services.registry import SERVICES
svc = SERVICES[ServiceType.<NAME>]
svc.config_model, svc.config_schema_version, svc.config_model_for(layer)
```

And run the audit below (item 9) before believing any claim about which layers a service
carries config on.

## 1. Identity and registration

- [ ] A `ServiceType` member, a `ServiceDefinition` in `ServiceAdapter.SERVICE_DEFINITIONS`, and
      one line in `opi/services/registry.py`.
- [ ] `uv run pytest tests/test_service_providers.py -q` passes. It fails if a `ServiceType` has
      no service, the registry has extras, or a definition drifted.
- [ ] The service name appears nowhere as a hardcoded contract: not in generic code, not in
      `opi/schemas/project_v2.json`. The global schema validates envelopes only.
      Legacy-input tolerance is the one exception and must carry a `comment` saying so.

## 2. Config model and schema fragment

- [ ] `config_model` and `config_schema_version` are either both set or both absent. A version
      without a model advertises a contract that does not exist; `tests/test_service_config_schema.py`
      enforces the pairing.
- [ ] `model_config = ConfigDict(extra="forbid", populate_by_name=True)`, so a typo fails loudly.
- [ ] Aliases match the key convention in the project file: dashes, not underscores.
- [ ] The committed fragment `catalog/<name>/<name>.v<version>.json` matches the model.
      Regenerate with `uv run python -m opi.services.config_schema` and commit; the drift lock
      is `tests/test_service_config_schema.py`.
- [ ] Required fields are derived from real production data, not from what feels required.
      Check against the project files before making a field mandatory.
- [ ] A service that carries genuinely different content per layer overrides
      `config_model_for(layer)`. Example: the storage services hold mount specs on the component
      but per-mount clone state on the deployment-component.
- [ ] Bumping `config_schema_version` means overriding `migrate_config` with the ordered steps
      from the old version to the current one. The machinery is there and wired
      (`validate_config` passes the version stamped on the entry through
      `service_entry_schema_version`), but no service has used it yet: every model is still at
      1.0. The first service to bump sets the pattern, so treat that step as new ground and
      test it against a real old-version block, not a synthetic one.
- [ ] Config shapes that recur across services live in `catalog/shared/` (`storage.py`,
      `revisions.py`), not copied into each package.

## 3. Config layers

- [ ] The layers the service actually carries config on are measured, not assumed (item 9).
- [ ] `config_api_fields(layer)` and `config_editables(layer)` are declared for those layers.
      A service whose config is read by a manager but declared on no layer is a gap: the config
      travels outside the service.
- [ ] `validate_service_configs` reaches those layers. It walks project, component, deployment
      and deployment-component. If a layer is not walked, the global schema is the only guard,
      and the global schema should not know about services.
- [ ] Paths are built with `config_path(...)`, never hardcoded strings.

## 4. Editables

Editables are where a service meets a human, so this is where a missing guardrail becomes a bad
project file.

- [ ] Every editable has a validator where the value set is constrained, and an enforcer where
      the constraint is about more than one field.
- [ ] Select where the value set is closed; free text only where the value is genuinely open.
      A select must be fed by an options provider that reads a real source. Verify that source
      has data: an options provider on an always-empty field renders an empty list.
- [ ] Optional fields carry `remove_when_none=True` plus a converter, so an empty form field
      leaves no key rather than writing `null`, `''` or `[]`. Writing the model default out
      instead freezes it: the project stays on the old value when the default changes.
- [ ] `remove_when_none` is NOT on a boolean whose default is `True`. The flag treats `False` as
      empty, so unticking it silently restores the default. This bit sleep-mode.
- [ ] An empty value that is a legitimate choice is offered as one, with help text, rather than
      being expressible only by leaving a field blank.
- [ ] Project-level service config sets `virtualize=("services", "_services-config")`, otherwise
      it collides with the service selection list in wizard state.
- [ ] The value the form writes is what you expect. Do not reason about it, submit the form and
      read the resulting YAML.

## 5. Reads and writes go through the service

- [ ] Managers validate through `provider.validate_config(...)`, not raw `dict.get()`.
- [ ] Service identity is resolved with `service_entry_name`, always. A services list holds bare
      strings, `{name}` records, `{reference}` records and legacy single-key dicts. A JSONPath on
      `@.reference` works only by convention and silently finds nothing on the other three forms.
- [ ] Config is read with `service_entry_config`. Note that `Project.service_config` and
      `service_entry_config` do not always agree on the same entry; know which one you need.
- [ ] A find-or-create reuses an existing entry and promotes a bare string in place. Appending a
      second entry for the same service makes the whole project file invalid, because a services
      list is a selection set.

## 6. Migration

- [ ] The migration is both version-gated and repeated unconditionally in `_fixup_v2_data`. A
      file already stamped at the new version never reaches the version-gated path, which is how
      dp-bn7 was silently blocked.
- [ ] One function decides where a block lives, used by both the migration and the runtime write
      path, so they cannot disagree. See `ensure_domains_config` in `connectors/subdomain.py`.
- [ ] Idempotent: a second `migrate_to_latest` returns `was_migrated=False`.
- [ ] Readers accept both the old and the new location for as long as unmigrated files exist.
- [ ] Removing the old shape from the global schema is a separate, later decision. Files do not
      rewrite themselves reliably: they only relocate when the project is processed, and many
      projects rarely are.

## 7. Tests

- [ ] Every regression test has been run against the unfixed code and observed to fail. A test
      that passes on both is worse than no test, because it reads as coverage. This happened
      twice in one session.
- [ ] Fixtures use real production shapes, not invented ones. An invented shape tests your
      imagination.
- [ ] The guardrail suite passes:

```bash
cd operations-manager/python
uv run pytest tests/test_service_providers.py tests/test_service_config_schema.py \
              tests/test_golden_manifests.py tests/test_flow_registry_snapshot.py -q
uv run ruff check . --fix && uv run ruff format . && uv run pyright
```

- [ ] The full suite ends on zero failures and zero errors. An error from a missing dependency
      or a polluting test is still a red suite.
- [ ] When a test breaks because the service changed, check whether its premise went stale
      before changing the assertion. Three tests used a service as the example of "a service
      without a config model" and were simply out of date.

## 8. UI

- [ ] The section actually renders. `hidden=True` on the service definition produces no UI at
      all, which is why sleep-mode was invisible in the wizard while fully working.
- [ ] A service whose config lives only on the component or deployment-component shows nothing
      in the project-wide step. That is by design, but the user must be told, otherwise ticking
      the service appears to do nothing.
- [ ] The detail-page block, if any, is behind the right role. Where the block reveals a secret
      (a link, a credential), that is an authorisation decision, not a display decision.
- [ ] Wizard and modal-edit both reach the section, and a validation failure is visible on the
      field it belongs to. A blocked save with an invisible error reads as a broken button.

## 9. Verify against real project files

The check that has settled every disagreement: read every production project file, migrate in
memory the way OPI does on load, and let each service validate its own block.

```python
for path in production_project_files:
    data, _ = migrate_to_latest(yaml.load(open(path)))
    validate_project_schema(data)      # what git_monitor and the save path run
    validate_service_configs(data)     # per-service models
```

- [ ] Zero failures after migration.
- [ ] Every occurrence of this service's config is claimed by its model. A block nothing
      validates is a gap, not an absence.
- [ ] Also validate raw, before migration, and compare against the previous schema. That is what
      `git_monitor` runs, and it is where a schema change breaks existing files.

## 10. Logging

A production log is the only witness you have afterwards. The rule of thumb: someone reading
`kubectl logs` should be able to reconstruct what the service did, for whom, without opening the
code.

- [ ] Every state-changing action logs one line at INFO naming the actor, the action and the
      subject: which service did what, for which project, deployment or realm. Two lines from
      this codebase that pass the test, and that are exactly what proved the reconcile path
      worked in the sandbox:

      Set required action UPDATE_PASSWORD to enabled=False in realm vlam-wt8-sandboxed-local
      Removed default role account:manage-account from realm vlam-wt8-sandboxed-local

- [ ] An idempotent no-op logs nothing. A line on every run turns the log into noise and makes
      change invisible; a silent second run is itself the evidence of idempotency.
- [ ] Values in the line are the identifying ones (names, counts, the flag that flipped), not
      whole objects. Never a config dict, a project dict or a credential: `bridge.py` dumped the
      entire project file including repository passwords on every render.
- [ ] DEBUG is not a safety valve. `utils/logging_config.py` pins the `opi` logger to DEBUG
      without an env switch, so a debug line reaches production stdout and Loki, where it lives
      far longer than in `kubectl logs`.
- [ ] A skip, a fallback or a partial result logs a WARNING saying what was skipped and why. The
      worst outcome is a silent skip: `assign_invite_permissions` puts an unassignable role in
      `not_found`, logs it, and still returns success, so the user sees a success page and hits
      the authorization wall later without knowing why.
- [ ] A failed operation logs an ERROR with the identifying context, and does not swallow the
      exception unless the swallowing is itself the designed behaviour and says so.
- [ ] Expected input handling is a WARNING, not an ERROR. A rejected project file is not
      ops-actionable and should not page anyone; see `git_monitor` for the pattern.
- [ ] The log distinguishes "nothing to do" from "could not do it". Those read the same in a
      badly worded line and mean opposite things.

## 11. Security and hygiene

- [ ] Provisioning and manifest generation are replay-safe; running twice changes nothing.
- [ ] `cleanup_manager_key` is set if the service creates server-side resources, and deletion
      actually removes them.
- [ ] Approvals, if the service has them, are declared through `config_approvals(layer)` and not
      special-cased elsewhere.
- [ ] Nothing in the service writes to a shared file outside the save chokepoint.

## 12. Documentation

- [ ] `features/<service>.md` exists: what it is, how to use it, configuration, examples,
      dependencies.
- [ ] Anything surprising found while auditing is written down where the next reader will look,
      not only in the review report.
