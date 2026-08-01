"""Typed config model for the ``postgresql-database`` service.

The only config this service carries is clone state, and it carries it on the deployment
layer: ``deployments[*].services[{postgresql-database}].config``. That state is written and
read by ``opi/manager/revision_manager.py``, not by a user, so there are no editables and no
visualizers here. It is modelled anyway because the service contract says a service declares
what may appear in its config block, and until now nothing did.

The shape itself is shared with ``minio-storage`` (see ``catalog/shared/revisions.py``); this
service only decides that it uses it.
"""

from __future__ import annotations

from opi.services.catalog.shared.revisions import CloneState


class PostgresqlDatabaseConfig(CloneState):
    """Clone state for a shared-instance PostgreSQL database."""
