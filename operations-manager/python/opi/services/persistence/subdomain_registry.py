"""publish-on-web's persistence: the subdomain registry.

The ``subdomain_registry`` table holds globally unique subdomain reservations across ALL
projects, so its rows span projects even though publish-on-web owns the logic.

Two representations of the same table coexist: :class:`SubdomainRegistry`, the ORM model
Alembic autogenerate targets (see ``migrations/env.py``), and
``SUBDOMAIN_REGISTRY_TABLE_SQL``, the raw CREATE the Alembic baseline migration used. Both
must stay in step, or autogenerate produces a change for a table that has not changed.

Queries run on the shared async engine in ``opi.core.db``, separate from the raw asyncpg
pools the rest of the app uses.
"""

from __future__ import annotations

import logging
from datetime import datetime  # noqa: TC003 (runtime-resolved by SQLAlchemy Mapped)
from typing import Any

from sqlalchemy import DateTime, Index, Integer, String, UniqueConstraint, delete, func, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Mapped, mapped_column

from opi.core.db import Base, session_scope
from opi.services.catalog.publish_on_web.subdomain import (
    BARE_DOMAIN_SUBDOMAIN,
    BaseDomainValidationError,
    SubdomainError,
    SubdomainNotAvailableError,
    SubdomainValidationError,
    validate_base_domain,
    validate_subdomain_for_domain,
)

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
    """Repository for the subdomain registry.

    Every subdomain + base_domain combination is unique across ALL projects. The register
    and change operations use ``ON CONFLICT`` and a single transaction to stay race-safe.
    """

    async def check_availability(self, subdomain: str, base_domain: str) -> bool:
        """Whether this subdomain is still free; a check is not a reservation."""
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
        """Register a new subdomain and return its registration.

        Raises SubdomainValidationError or BaseDomainValidationError on a bad name,
        SubdomainNotAvailableError when the name is gone, SubdomainError on anything else.
        Registering the name this project already holds returns that registration.
        """
        is_valid, error_message = validate_subdomain_for_domain(subdomain, base_domain, cluster)
        if not is_valid:
            raise SubdomainValidationError(error_message)

        subdomain_lower = subdomain.lower()
        base_domain_lower = base_domain.lower()

        is_valid, error_message = validate_base_domain(base_domain_lower, cluster)
        if not is_valid:
            raise BaseDomainValidationError(error_message)

        # The availability check is for the message only; the ON CONFLICT below is what makes
        # two simultaneous registrations safe. The message stays generic, so it cannot say
        # whether another project holds the name or the platform reserves it.
        if not await self.check_availability(subdomain_lower, base_domain_lower):
            raise SubdomainNotAvailableError(f"Subdomein '{subdomain_lower}.{base_domain_lower}' is niet beschikbaar")

        try:
            async with session_scope() as session:
                # RETURNING id yields None when the row already existed.
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
                    existing = await self.get_by_subdomain(subdomain_lower, base_domain_lower)
                    if existing and existing.get("project_name") == project_name:
                        logger.info(
                            f"Subdomain '{subdomain_lower}.{base_domain_lower}' already registered "
                            f"to same project '{project_name}'"
                        )
                        return existing
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
        """The registration of this subdomain, or None when it is free."""
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
        """Every registration this project holds, ordered by name."""
        async with session_scope() as session:
            result = await session.execute(
                select(SubdomainRegistry)
                .where(SubdomainRegistry.project_name == project_name)
                .order_by(SubdomainRegistry.subdomain, SubdomainRegistry.base_domain)
            )
            return [row.to_dict() for row in result.scalars().all()]

    async def get_by_deployment(self, project_name: str, deployment_name: str) -> dict[str, Any] | None:
        """The registration of this deployment, or None when it has none."""
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
        """Delete this registration; False when there was nothing to delete."""
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
        """Delete every registration of this project and return how many went."""
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
        """Delete every registration of this deployment and return how many went."""
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
        """Register the apex of a domain and return its registration.

        The subdomain value is ``@``, the DNS convention for the apex, and the subdomain
        format check is skipped because ``@`` is not a DNS label.

        Raises BaseDomainValidationError on a bad domain, SubdomainNotAvailableError when the
        apex is gone, SubdomainError on anything else.
        """
        base_domain_lower = base_domain.lower()

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

    async def delete_bare_domain(self, base_domain: str, project_name: str | None = None) -> bool:
        """Delete an apex registration; False when there was nothing of this project to delete.

        The registry is shared across tenants and a base domain is just a string in a project
        file, so a caller that cleans up after a deployment MUST pass ``project_name``.
        Without it, naming another tenant's domain wipes THEIR apex registration.
        """
        if project_name is None:
            return await self.delete(BARE_DOMAIN_SUBDOMAIN, base_domain)

        async with session_scope() as session:
            result = await session.execute(
                delete(SubdomainRegistry).where(
                    SubdomainRegistry.subdomain == BARE_DOMAIN_SUBDOMAIN,
                    SubdomainRegistry.base_domain == base_domain.lower(),
                    SubdomainRegistry.project_name == project_name,
                )
            )
            deleted = result.rowcount == 1
            if deleted:
                logger.info(f"Deleted bare domain registration '{base_domain.lower()}' for project '{project_name}'")
                audit_logger.info(f"BARE_DOMAIN_DELETED: {base_domain.lower()} project={project_name}")
            return deleted

    async def register_or_update_for_deployment(
        self,
        subdomain: str,
        base_domain: str,
        project_name: str,
        deployment_name: str,
        cluster: str,
        created_by: str | None = None,
    ) -> dict[str, Any]:
        """Give this deployment the subdomain it asks for, and return the registration.

        An unchanged subdomain is returned as it stands; a changed one is swapped in one
        transaction, so a new name that turns out to be gone leaves the old one in place.

        Raises SubdomainValidationError or BaseDomainValidationError on a bad name, and
        SubdomainNotAvailableError when another project holds the new one.
        """
        is_valid, error_message = validate_subdomain_for_domain(subdomain, base_domain, cluster)
        if not is_valid:
            raise SubdomainValidationError(error_message)

        subdomain_lower = subdomain.lower()
        base_domain_lower = base_domain.lower()

        is_valid, error_message = validate_base_domain(base_domain_lower, cluster)
        if not is_valid:
            raise BaseDomainValidationError(error_message)

        existing = await self.get_by_deployment(project_name, deployment_name)

        if existing:
            if existing["subdomain"] == subdomain_lower and existing["base_domain"] == base_domain_lower:
                logger.debug(
                    f"Subdomain '{subdomain_lower}.{base_domain_lower}' unchanged for "
                    f"deployment '{project_name}/{deployment_name}'"
                )
                return existing

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
        """Swap a deployment's subdomain in one transaction, and return the new registration.

        Raises SubdomainNotAvailableError when the new subdomain is gone. ``session_scope`` is
        one transaction that rolls back on any exception, so the old subdomain survives a
        failed swap.
        """
        try:
            async with session_scope() as session:
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

                await session.execute(
                    delete(SubdomainRegistry).where(
                        SubdomainRegistry.subdomain == old_subdomain,
                        SubdomainRegistry.base_domain == old_base_domain,
                    )
                )

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
        """Move a registration to another deployment or cluster; None when it does not exist."""
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
        """How many registrations the registry holds, over all projects."""
        async with session_scope() as session:
            result = await session.execute(select(func.count()).select_from(SubdomainRegistry))
            return result.scalar_one() or 0

    async def list_all(self, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        """One page of registrations over all projects, ordered by name."""
        async with session_scope() as session:
            result = await session.execute(
                select(SubdomainRegistry)
                .order_by(SubdomainRegistry.subdomain, SubdomainRegistry.base_domain)
                .limit(limit)
                .offset(offset)
            )
            return [row.to_dict() for row in result.scalars().all()]


def create_subdomain_connector() -> SubdomainConnector:
    return SubdomainConnector()


#: The raw CREATE the Alembic baseline migration used; keep it in step with the ORM model.
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
