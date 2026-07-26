"""publish-on-web's persistence: the subdomain registry (RC-5).

Owned by the publish-on-web service. The ``subdomain_registry`` table tracks globally
unique subdomain reservations across ALL projects (global uniqueness is the point), so
the rows span projects even though the ownership/logic is the service's.

Two representations of the same table coexist during phased ORM adoption:
- :class:`SubdomainRegistry` -- the ORM model (schema-as-code); Alembic autogenerate
  targets it (see ``migrations/env.py``), so future schema changes are generated.
- ``SUBDOMAIN_REGISTRY_TABLE_SQL`` -- the raw CREATE used by the Alembic baseline
  migration that first created the table.
Queries run through the SQLAlchemy ORM (:class:`SubdomainConnector`) on the shared async
engine in ``opi.core.db`` -- separate from the raw asyncpg pools the rest of the app
uses. Shared subdomain validation helpers stay in ``opi/connectors/subdomain.py`` and
are imported here.
"""

from __future__ import annotations

import logging
from datetime import datetime  # noqa: TC003 (runtime-resolved by SQLAlchemy Mapped)
from typing import Any

from sqlalchemy import DateTime, Index, Integer, String, UniqueConstraint, delete, func, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Mapped, mapped_column

from opi.connectors.subdomain import (
    BARE_DOMAIN_SUBDOMAIN,
    BaseDomainValidationError,
    SubdomainError,
    SubdomainNotAvailableError,
    SubdomainValidationError,
    validate_base_domain,
    validate_subdomain,
)
from opi.core.db import Base, session_scope

logger = logging.getLogger(__name__)
# Audit logger kept under the same name as before the move, so log routing is unchanged.
audit_logger = logging.getLogger("opi.audit.subdomain")


class SubdomainRegistry(Base):
    """ORM model for the ``subdomain_registry`` table (schema-as-code).

    Matches ``SUBDOMAIN_REGISTRY_TABLE_SQL`` exactly; ``alembic revision --autogenerate``
    must produce NO changes for this table.
    """

    __tablename__ = "subdomain_registry"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subdomain: Mapped[str] = mapped_column(String(63), nullable=False)
    base_domain: Mapped[str] = mapped_column(String(255), nullable=False)
    project_name: Mapped[str] = mapped_column(String(63), nullable=False)
    deployment_name: Mapped[str] = mapped_column(String(63), nullable=False)
    cluster: Mapped[str] = mapped_column(String(63), nullable=False)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=True
    )
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)

    __table_args__ = (
        UniqueConstraint("subdomain", "base_domain", name="subdomain_registry_subdomain_base_domain_key"),
        Index("idx_subdomain_project", "project_name"),
        Index("idx_subdomain_deployment", "project_name", "deployment_name"),
    )

    def to_dict(self) -> dict[str, Any]:
        """Row as the flat dict the connector's callers expect (unchanged public shape)."""
        return {
            "id": self.id,
            "subdomain": self.subdomain,
            "base_domain": self.base_domain,
            "project_name": self.project_name,
            "deployment_name": self.deployment_name,
            "cluster": self.cluster,
            "created_at": self.created_at,
            "created_by": self.created_by,
        }


class SubdomainConnector:
    """Repository for the subdomain registry (globally unique subdomains for nice URLs).

    Each subdomain + base_domain combination must be unique across ALL projects. Queries
    run through the SQLAlchemy ORM (:class:`SubdomainRegistry`) on the shared async engine
    (``opi.core.db``); the atomic register / change operations use ``ON CONFLICT`` and a
    single transaction to stay race-safe.
    """

    async def check_availability(self, subdomain: str, base_domain: str) -> bool:
        """Check if a subdomain is available for registration.

        Args:
            subdomain: The subdomain to check (e.g., "myapp")
            base_domain: The base domain (e.g., "rijks.app")

        Returns:
            True if the subdomain is available, False if already taken
        """
        async with session_scope() as session:
            result = await session.execute(
                select(SubdomainRegistry.id).where(
                    SubdomainRegistry.subdomain == subdomain.lower(),
                    SubdomainRegistry.base_domain == base_domain.lower(),
                )
            )
            return result.first() is None

    async def register(
        self,
        subdomain: str,
        base_domain: str,
        project_name: str,
        deployment_name: str,
        cluster: str,
        created_by: str | None = None,
    ) -> dict[str, Any]:
        """Register a new subdomain.

        Args:
            subdomain: The subdomain to register (e.g., "myapp")
            base_domain: The base domain (e.g., "rijks.app")
            project_name: The project name that owns this subdomain
            deployment_name: The deployment name using this subdomain
            cluster: The cluster where this subdomain is deployed
            created_by: Optional email/identifier of who created the registration

        Returns:
            Dictionary with the created registration details

        Raises:
            SubdomainValidationError: If the subdomain format is invalid
            BaseDomainValidationError: If the base domain is not supported
            SubdomainNotAvailableError: If the subdomain is already taken
            SubdomainError: If registration fails
        """
        # Validate subdomain format
        is_valid, error_message = validate_subdomain(subdomain)
        if not is_valid:
            raise SubdomainValidationError(error_message)

        subdomain_lower = subdomain.lower()
        base_domain_lower = base_domain.lower()

        # Validate base domain against supported domains for this cluster
        is_valid, error_message = validate_base_domain(base_domain_lower, cluster)
        if not is_valid:
            raise BaseDomainValidationError(error_message)

        # TOCTOU Protection Strategy:
        # We use a two-layer defense against race conditions:
        #
        # Layer 1 (UX): check_availability provides early, user-friendly error messages
        # with context about why registration failed. This catches 99.9% of conflicts.
        #
        # Layer 2 (Security): INSERT...ON CONFLICT DO NOTHING provides atomic protection
        # against the rare case where two requests pass Layer 1 simultaneously.
        # If ON CONFLICT triggers, we detect it via NULL result and handle accordingly.
        #
        # This approach gives us both good UX (informative errors) and security (atomic ops).
        if not await self.check_availability(subdomain_lower, base_domain_lower):
            # Use generic error message to prevent information disclosure
            # (don't reveal whether subdomain is taken by another project or reserved)
            raise SubdomainNotAvailableError(f"Subdomein '{subdomain_lower}.{base_domain_lower}' is niet beschikbaar")

        try:
            async with session_scope() as session:
                # Atomic INSERT with ON CONFLICT - the true protection against TOCTOU races.
                # RETURNING id yields None when the row already existed (conflict), which we
                # handle below.
                inserted_id = (
                    await session.execute(
                        pg_insert(SubdomainRegistry)
                        .values(
                            subdomain=subdomain_lower,
                            base_domain=base_domain_lower,
                            project_name=project_name,
                            deployment_name=deployment_name,
                            cluster=cluster,
                            created_by=created_by,
                        )
                        .on_conflict_do_nothing(index_elements=["subdomain", "base_domain"])
                        .returning(SubdomainRegistry.id)
                    )
                ).scalar_one_or_none()

                if inserted_id is None:
                    # Conflict occurred - subdomain was taken between check and register.
                    existing = await self.get_by_subdomain(subdomain_lower, base_domain_lower)
                    if existing and existing.get("project_name") == project_name:
                        logger.info(
                            f"Subdomain '{subdomain_lower}.{base_domain_lower}' already registered "
                            f"to same project '{project_name}'"
                        )
                        return existing
                    # Generic error message to prevent information disclosure.
                    raise SubdomainNotAvailableError(
                        f"Subdomein '{subdomain_lower}.{base_domain_lower}' is niet beschikbaar"
                    )

                row = (
                    await session.execute(select(SubdomainRegistry).where(SubdomainRegistry.id == inserted_id))
                ).scalar_one()
                logger.info(
                    f"Registered subdomain '{subdomain_lower}.{base_domain_lower}' "
                    f"for project '{project_name}', deployment '{deployment_name}'"
                )
                audit_logger.info(
                    f"SUBDOMAIN_REGISTERED: {subdomain_lower}.{base_domain_lower} "
                    f"project={project_name} deployment={deployment_name} cluster={cluster}"
                )
                return row.to_dict()
        except SubdomainNotAvailableError:
            raise
        except Exception as e:
            logger.exception(f"Failed to register subdomain '{subdomain_lower}.{base_domain_lower}'")
            raise SubdomainError(f"Subdomain registration failed: {e}") from e

    async def get_by_subdomain(self, subdomain: str, base_domain: str) -> dict[str, Any] | None:
        """Get a subdomain registration by subdomain and base domain.

        Args:
            subdomain: The subdomain to look up
            base_domain: The base domain

        Returns:
            Dictionary with registration details, or None if not found
        """
        async with session_scope() as session:
            row = (
                await session.execute(
                    select(SubdomainRegistry).where(
                        SubdomainRegistry.subdomain == subdomain.lower(),
                        SubdomainRegistry.base_domain == base_domain.lower(),
                    )
                )
            ).scalar_one_or_none()
            return row.to_dict() if row else None

    async def get_by_project(self, project_name: str) -> list[dict[str, Any]]:
        """Get all subdomain registrations for a project.

        Args:
            project_name: The project name to look up

        Returns:
            List of registration dictionaries
        """
        async with session_scope() as session:
            result = await session.execute(
                select(SubdomainRegistry)
                .where(SubdomainRegistry.project_name == project_name)
                .order_by(SubdomainRegistry.subdomain, SubdomainRegistry.base_domain)
            )
            return [row.to_dict() for row in result.scalars().all()]

    async def get_by_deployment(self, project_name: str, deployment_name: str) -> dict[str, Any] | None:
        """Get subdomain registration for a specific deployment.

        Args:
            project_name: The project name
            deployment_name: The deployment name

        Returns:
            Registration dictionary, or None if not found
        """
        async with session_scope() as session:
            row = (
                await session.execute(
                    select(SubdomainRegistry).where(
                        SubdomainRegistry.project_name == project_name,
                        SubdomainRegistry.deployment_name == deployment_name,
                    )
                )
            ).scalar_one_or_none()
            return row.to_dict() if row else None

    async def delete(self, subdomain: str, base_domain: str) -> bool:
        """Delete a subdomain registration.

        Args:
            subdomain: The subdomain to delete
            base_domain: The base domain

        Returns:
            True if deleted, False if not found
        """
        async with session_scope() as session:
            result = await session.execute(
                delete(SubdomainRegistry).where(
                    SubdomainRegistry.subdomain == subdomain.lower(),
                    SubdomainRegistry.base_domain == base_domain.lower(),
                )
            )
            deleted = result.rowcount == 1
            if deleted:
                logger.info(f"Deleted subdomain registration '{subdomain.lower()}.{base_domain.lower()}'")
                audit_logger.info(f"SUBDOMAIN_DELETED: {subdomain.lower()}.{base_domain.lower()}")
            return deleted

    async def delete_by_project(self, project_name: str) -> int:
        """Delete all subdomain registrations for a project.

        Args:
            project_name: The project name

        Returns:
            Number of registrations deleted
        """
        async with session_scope() as session:
            result = await session.execute(
                delete(SubdomainRegistry).where(SubdomainRegistry.project_name == project_name)
            )
            count = result.rowcount
            if count > 0:
                logger.info(f"Deleted {count} subdomain registration(s) for project '{project_name}'")
                audit_logger.info(f"SUBDOMAINS_DELETED_BY_PROJECT: project={project_name} count={count}")
            return count

    async def delete_by_deployment(self, project_name: str, deployment_name: str) -> int:
        """Delete all subdomain registrations for a specific deployment.

        Args:
            project_name: The project name
            deployment_name: The deployment name

        Returns:
            Number of registrations deleted
        """
        async with session_scope() as session:
            result = await session.execute(
                delete(SubdomainRegistry).where(
                    SubdomainRegistry.project_name == project_name,
                    SubdomainRegistry.deployment_name == deployment_name,
                )
            )
            count = result.rowcount
            if count > 0:
                logger.info(
                    f"Deleted {count} subdomain registration(s) for deployment '{project_name}/{deployment_name}'"
                )
                audit_logger.info(
                    f"SUBDOMAINS_DELETED_BY_DEPLOYMENT: project={project_name} "
                    f"deployment={deployment_name} count={count}"
                )
            return count

    async def register_bare_domain(
        self,
        base_domain: str,
        project_name: str,
        deployment_name: str,
        cluster: str,
        created_by: str | None = None,
    ) -> dict[str, Any]:
        """Register the bare/apex domain in the subdomain registry.

        Uses ``@`` as the subdomain value (DNS convention for apex).
        Skips subdomain format validation since ``@`` is not a DNS label.

        Args:
            base_domain: The custom domain (e.g., "voorbeeld.nl")
            project_name: The project name that owns this domain
            deployment_name: The deployment name using this domain
            cluster: The cluster where this domain is deployed
            created_by: Optional email/identifier of who created the registration

        Returns:
            Dictionary with the created registration details

        Raises:
            BaseDomainValidationError: If the base domain is not valid
            SubdomainNotAvailableError: If the bare domain is already taken
            SubdomainError: If registration fails
        """
        base_domain_lower = base_domain.lower()

        # Validate base domain syntax
        is_valid, error_message = validate_base_domain(base_domain_lower, cluster)
        if not is_valid:
            raise BaseDomainValidationError(error_message)

        if not await self.check_availability(BARE_DOMAIN_SUBDOMAIN, base_domain_lower):
            existing = await self.get_by_subdomain(BARE_DOMAIN_SUBDOMAIN, base_domain_lower)
            if existing and existing.get("project_name") == project_name:
                logger.info(f"Bare domain '{base_domain_lower}' already registered to same project '{project_name}'")
                return existing
            raise SubdomainNotAvailableError(f"Kaal domein '{base_domain_lower}' is niet beschikbaar")

        try:
            async with session_scope() as session:
                inserted_id = (
                    await session.execute(
                        pg_insert(SubdomainRegistry)
                        .values(
                            subdomain=BARE_DOMAIN_SUBDOMAIN,
                            base_domain=base_domain_lower,
                            project_name=project_name,
                            deployment_name=deployment_name,
                            cluster=cluster,
                            created_by=created_by,
                        )
                        .on_conflict_do_nothing(index_elements=["subdomain", "base_domain"])
                        .returning(SubdomainRegistry.id)
                    )
                ).scalar_one_or_none()

                if inserted_id is None:
                    existing = await self.get_by_subdomain(BARE_DOMAIN_SUBDOMAIN, base_domain_lower)
                    if existing and existing.get("project_name") == project_name:
                        return existing
                    raise SubdomainNotAvailableError(f"Kaal domein '{base_domain_lower}' is niet beschikbaar")

                row = (
                    await session.execute(select(SubdomainRegistry).where(SubdomainRegistry.id == inserted_id))
                ).scalar_one()
                logger.info(
                    f"Registered bare domain '{base_domain_lower}' "
                    f"for project '{project_name}', deployment '{deployment_name}'"
                )
                audit_logger.info(
                    f"BARE_DOMAIN_REGISTERED: {base_domain_lower} "
                    f"project={project_name} deployment={deployment_name} cluster={cluster}"
                )
                return row.to_dict()
        except SubdomainNotAvailableError:
            raise
        except Exception as e:
            logger.exception(f"Failed to register bare domain '{base_domain_lower}'")
            raise SubdomainError(f"Bare domain registration failed: {e}") from e

    async def delete_bare_domain(self, base_domain: str) -> bool:
        """Delete a bare domain registration.

        Args:
            base_domain: The base domain to delete

        Returns:
            True if deleted, False if not found
        """
        return await self.delete(BARE_DOMAIN_SUBDOMAIN, base_domain)

    async def register_or_update_for_deployment(
        self,
        subdomain: str,
        base_domain: str,
        project_name: str,
        deployment_name: str,
        cluster: str,
        created_by: str | None = None,
    ) -> dict[str, Any]:
        """Register a subdomain or update if the deployment's subdomain has changed.

        This method handles the case where a deployment's subdomain configuration changes.
        It uses a database transaction to ensure atomicity - if the new subdomain registration
        fails, the old subdomain is preserved (not lost).

        The method will:
        1. Check if the deployment already has a subdomain registration
        2. If the subdomain hasn't changed, return the existing registration
        3. If the subdomain has changed, atomically delete the old and insert the new
           within a transaction to prevent data loss on failure

        Args:
            subdomain: The new/current subdomain
            base_domain: The base domain
            project_name: The project name
            deployment_name: The deployment name
            cluster: The cluster
            created_by: Optional creator identifier

        Returns:
            Dictionary with registration details

        Raises:
            SubdomainValidationError: If the subdomain format is invalid
            BaseDomainValidationError: If the base domain is not supported
            SubdomainNotAvailableError: If the new subdomain is already taken by another project
        """
        # Validate subdomain format first (before any DB operations)
        is_valid, error_message = validate_subdomain(subdomain)
        if not is_valid:
            raise SubdomainValidationError(error_message)

        subdomain_lower = subdomain.lower()
        base_domain_lower = base_domain.lower()

        # Validate base domain against supported domains for this cluster
        is_valid, error_message = validate_base_domain(base_domain_lower, cluster)
        if not is_valid:
            raise BaseDomainValidationError(error_message)

        # Check if this deployment already has a registration
        existing = await self.get_by_deployment(project_name, deployment_name)

        if existing:
            # Check if subdomain has changed
            if existing["subdomain"] == subdomain_lower and existing["base_domain"] == base_domain_lower:
                # No change - return existing
                logger.debug(
                    f"Subdomain '{subdomain_lower}.{base_domain_lower}' unchanged for "
                    f"deployment '{project_name}/{deployment_name}'"
                )
                return existing

            # Subdomain changed - use atomic transaction to prevent data loss
            # If new subdomain registration fails, old subdomain is preserved
            logger.info(
                f"Subdomain changed from '{existing['subdomain']}.{existing['base_domain']}' to "
                f"'{subdomain_lower}.{base_domain_lower}' for deployment '{project_name}/{deployment_name}'"
            )

            return await self._atomic_subdomain_change(
                old_subdomain=existing["subdomain"],
                old_base_domain=existing["base_domain"],
                new_subdomain=subdomain_lower,
                new_base_domain=base_domain_lower,
                project_name=project_name,
                deployment_name=deployment_name,
                cluster=cluster,
                created_by=created_by,
            )

        # No existing registration - register the new subdomain
        return await self.register(
            subdomain=subdomain,
            base_domain=base_domain,
            project_name=project_name,
            deployment_name=deployment_name,
            cluster=cluster,
            created_by=created_by,
        )

    async def _atomic_subdomain_change(
        self,
        old_subdomain: str,
        old_base_domain: str,
        new_subdomain: str,
        new_base_domain: str,
        project_name: str,
        deployment_name: str,
        cluster: str,
        created_by: str | None = None,
    ) -> dict[str, Any]:
        """Atomically change a deployment's subdomain within a transaction.

        This ensures that if the new subdomain registration fails (e.g., already taken),
        the old subdomain is preserved and not lost.

        Args:
            old_subdomain: The existing subdomain to delete
            old_base_domain: The existing base domain
            new_subdomain: The new subdomain to register
            new_base_domain: The new base domain
            project_name: The project name
            deployment_name: The deployment name
            cluster: The cluster
            created_by: Optional creator identifier

        Returns:
            Dictionary with the new registration details

        Raises:
            SubdomainNotAvailableError: If the new subdomain is already taken
        """
        try:
            # session_scope is one transaction: on any exception it rolls back, so the
            # old subdomain is preserved if the new one turns out to be taken.
            async with session_scope() as session:
                # Step 1: new subdomain available? (within the transaction)
                taken = (
                    await session.execute(
                        select(SubdomainRegistry.id).where(
                            SubdomainRegistry.subdomain == new_subdomain,
                            SubdomainRegistry.base_domain == new_base_domain,
                        )
                    )
                ).first()
                if taken is not None:
                    raise SubdomainNotAvailableError(
                        f"Subdomein '{new_subdomain}.{new_base_domain}' is niet beschikbaar"
                    )

                # Step 2: delete the old registration
                await session.execute(
                    delete(SubdomainRegistry).where(
                        SubdomainRegistry.subdomain == old_subdomain,
                        SubdomainRegistry.base_domain == old_base_domain,
                    )
                )

                # Step 3: insert the new registration
                inserted_id = (
                    await session.execute(
                        insert(SubdomainRegistry)
                        .values(
                            subdomain=new_subdomain,
                            base_domain=new_base_domain,
                            project_name=project_name,
                            deployment_name=deployment_name,
                            cluster=cluster,
                            created_by=created_by,
                        )
                        .returning(SubdomainRegistry.id)
                    )
                ).scalar_one()
                row = (
                    await session.execute(select(SubdomainRegistry).where(SubdomainRegistry.id == inserted_id))
                ).scalar_one()
                result_dict = row.to_dict()

            logger.info(
                f"Atomically changed subdomain from '{old_subdomain}.{old_base_domain}' to "
                f"'{new_subdomain}.{new_base_domain}' for deployment '{project_name}/{deployment_name}'"
            )
            audit_logger.info(
                f"SUBDOMAIN_CHANGED: {old_subdomain}.{old_base_domain} -> {new_subdomain}.{new_base_domain} "
                f"project={project_name} deployment={deployment_name} cluster={cluster}"
            )
            return result_dict

        except SubdomainNotAvailableError:
            raise
        except Exception as e:
            logger.exception(
                f"Failed to atomically change subdomain from '{old_subdomain}.{old_base_domain}' to "
                f"'{new_subdomain}.{new_base_domain}'"
            )
            raise SubdomainError(f"Subdomain change failed: {e}") from e

    async def update(
        self,
        subdomain: str,
        base_domain: str,
        deployment_name: str | None = None,
        cluster: str | None = None,
    ) -> dict[str, Any] | None:
        """Update a subdomain registration.

        Args:
            subdomain: The subdomain to update
            base_domain: The base domain
            deployment_name: New deployment name (optional)
            cluster: New cluster (optional)

        Returns:
            Updated registration dictionary, or None if not found
        """
        values: dict[str, Any] = {}
        if deployment_name is not None:
            values["deployment_name"] = deployment_name
        if cluster is not None:
            values["cluster"] = cluster

        if not values:
            return await self.get_by_subdomain(subdomain, base_domain)

        async with session_scope() as session:
            result = await session.execute(
                update(SubdomainRegistry)
                .where(
                    SubdomainRegistry.subdomain == subdomain.lower(),
                    SubdomainRegistry.base_domain == base_domain.lower(),
                )
                .values(**values)
            )
            if result.rowcount == 0:
                return None
        # Read back the updated row (committed above) so the returned dict is current.
        return await self.get_by_subdomain(subdomain, base_domain)

    async def count_all(self) -> int:
        """Count total number of subdomain registrations.

        Returns:
            Total count of registrations
        """
        async with session_scope() as session:
            result = await session.execute(select(func.count()).select_from(SubdomainRegistry))
            return result.scalar_one() or 0

    async def list_all(self, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        """List all subdomain registrations.

        Args:
            limit: Maximum number of results to return
            offset: Number of results to skip

        Returns:
            List of registration dictionaries
        """
        async with session_scope() as session:
            result = await session.execute(
                select(SubdomainRegistry)
                .order_by(SubdomainRegistry.subdomain, SubdomainRegistry.base_domain)
                .limit(limit)
                .offset(offset)
            )
            return [row.to_dict() for row in result.scalars().all()]


# Factory function
def create_subdomain_connector() -> SubdomainConnector:
    """Create a SubdomainConnector instance.

    Returns:
        SubdomainConnector instance
    """
    return SubdomainConnector()


# SQL for table creation (used by startup)
SUBDOMAIN_REGISTRY_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS subdomain_registry (
    id SERIAL PRIMARY KEY,
    subdomain VARCHAR(63) NOT NULL,
    base_domain VARCHAR(255) NOT NULL,
    project_name VARCHAR(63) NOT NULL,
    deployment_name VARCHAR(63) NOT NULL,
    cluster VARCHAR(63) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    created_by VARCHAR(255),
    UNIQUE (subdomain, base_domain)
);

CREATE INDEX IF NOT EXISTS idx_subdomain_project ON subdomain_registry(project_name);
CREATE INDEX IF NOT EXISTS idx_subdomain_deployment ON subdomain_registry(project_name, deployment_name);
"""
