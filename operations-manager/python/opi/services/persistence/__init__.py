"""Service-owned ORM models (RC-5). Importing this package registers every model on
``opi.core.db.Base.metadata`` so Alembic autogenerate can see them.
"""

from opi.services.persistence.marked_for_deletion import MarkedForDeletion  # noqa: F401
from opi.services.persistence.runs import Run  # noqa: F401
from opi.services.persistence.subdomain_registry import SubdomainRegistry  # noqa: F401
from opi.services.persistence.users import User  # noqa: F401
