"""
Application readiness state tracking.

Tracks which critical services (database, Keycloak, OAuth) are available.
Used by the maintenance middleware to show a status page when the app
is not fully ready, and by health endpoints for Kubernetes probes.
"""

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

logger = logging.getLogger(__name__)


@dataclass
class ServiceStatus:
    """Status of a single service dependency."""

    name: str
    display_name: str
    ready: bool = False
    error: str | None = None
    last_check: datetime | None = None

    def mark_ready(self) -> None:
        self.ready = True
        self.error = None
        self.last_check = datetime.now(UTC)
        logger.info(f"Service '{self.display_name}' is now ready")

    def mark_failed(self, error: str) -> None:
        self.ready = False
        self.error = error
        self.last_check = datetime.now(UTC)
        logger.warning(f"Service '{self.display_name}' is unavailable: {error}")


@dataclass
class ReadinessState:
    """Tracks readiness of all critical service dependencies."""

    database: ServiceStatus = field(default_factory=lambda: ServiceStatus("database", "Database (PostgreSQL)"))
    keycloak: ServiceStatus = field(default_factory=lambda: ServiceStatus("keycloak", "Keycloak (Authentication)"))
    oauth: ServiceStatus = field(default_factory=lambda: ServiceStatus("oauth", "OAuth Client"))
    projects: ServiceStatus = field(default_factory=lambda: ServiceStatus("projects", "Project Files (Git)"))

    @property
    def is_ready(self) -> bool:
        """True when all critical services are available."""
        return self.database.ready and self.keycloak.ready and self.oauth.ready and self.projects.ready

    def get_unavailable_services(self) -> list[ServiceStatus]:
        """Return list of services that are not ready."""
        return [s for s in [self.database, self.keycloak, self.oauth, self.projects] if not s.ready]

    def get_all_services(self) -> list[ServiceStatus]:
        """Return all tracked services."""
        return [self.database, self.keycloak, self.oauth, self.projects]

    def summary(self) -> dict[str, bool]:
        """Return a dict summary of service readiness."""
        return {s.name: s.ready for s in self.get_all_services()}


# Global singleton
_state: ReadinessState | None = None


def get_readiness_state() -> ReadinessState:
    global _state
    if _state is None:
        _state = ReadinessState()
    return _state
