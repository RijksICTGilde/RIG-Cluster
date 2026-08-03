"""Config model for the ``namespace-postgresql-database`` service (RC-5 Phase 2).

This is the first service converted to a typed config model. It faithfully mirrors
today's behaviour in ``DatabaseManager`` (the ~130 lines of ``dict.get()`` +
hand-rolled validation building ``DEFAULT_CONFIG``): same defaults, same required
fields, same privilege allow-list. Version ``1.0`` deliberately *describes reality*
rather than tightening it, so every existing project file validates unchanged; any
stricter guardrails (e.g. a storage-quantity pattern) come later as a versioned
``migrate_config`` step, never as a silent behaviour change on the current version.

The CNPG-cluster field set itself lives in ``catalog/shared/postgres.py`` because it
is now shared with ``postgresql-database``'s ``scope: project`` variant; this service
only decides that it uses it.
"""

from __future__ import annotations

from pydantic import ConfigDict

from opi.services.catalog.shared.postgres import DedicatedPostgresFields


class NamespacePostgresConfig(DedicatedPostgresFields):
    """Typed config for a dedicated (namespace) PostgreSQL cluster.

    Field defaults reproduce ``DatabaseManager.DEFAULT_CONFIG`` so validating an
    existing (untyped) config through this model yields the same merged result the
    old ``dict.get()`` merge produced.
    """

    # Re-declared explicitly: an empty subclass does not re-emit the parent's
    # extra="forbid" into its JSON schema (additionalProperties: false), and that
    # guardrail is what rejects config typos (e.g. "instaces").
    model_config = ConfigDict(extra="forbid")
