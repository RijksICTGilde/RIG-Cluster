"""Config model for the ``namespace-postgresql-database`` service.

Version ``1.0`` *describes reality* rather than tightening it, so every existing project
file validates unchanged. A stricter guardrail (a storage-quantity pattern, say) is a
versioned ``migrate_config`` step, never a silent behaviour change on the current version.

The CNPG-cluster field set itself lives in ``catalog/shared/postgres.py`` because it is
shared with ``postgresql-database``'s ``scope: project`` variant; this service only decides
that it uses it.
"""

from __future__ import annotations

from pydantic import ConfigDict

from opi.services.catalog.shared.postgres import DedicatedPostgresFields


class NamespacePostgresConfig(DedicatedPostgresFields):
    """Typed config for a dedicated (namespace) PostgreSQL cluster.

    Field defaults reproduce ``DatabaseManager.DEFAULT_CONFIG``, so an existing untyped
    config validates to the same merged result.
    """

    # Re-declared explicitly: an empty subclass does not re-emit the parent's
    # extra="forbid" into its JSON schema (additionalProperties: false), and that
    # guardrail is what rejects config typos (e.g. "instaces").
    model_config = ConfigDict(extra="forbid")
