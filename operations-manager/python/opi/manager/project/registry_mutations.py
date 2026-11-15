"""Writing container-registry entries into the project file."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ruamel.yaml.scalarstring import LiteralScalarString

from opi.services import ServiceType
from opi.services.project import Project
from opi.utils.age import encrypt_age_content, get_project_public_key

if TYPE_CHECKING:
    from opi.manager.project.access import ProjectFileAccess

logger = logging.getLogger(__name__)


class RegistryMutations:
    """The registry writers of ``ProjectManager``, built per call around its host."""

    def __init__(self, host: ProjectFileAccess) -> None:
        self._host = host

    def _upsert_registry_entry(self, project_data: dict[str, Any], entry: dict[str, Any]) -> bool:
        """Write a registry into the config of the image-registries service. True when new."""
        service_name = ServiceType.IMAGE_REGISTRIES.value
        project = Project(project_data)
        config = project.service_config(service_name)
        registries = list(config.get("registries", [])) if isinstance(config, dict) else []

        created = True
        for index, existing in enumerate(registries):
            if isinstance(existing, dict) and existing.get("name") == entry["name"]:
                registries[index] = entry
                created = False
                break
        else:
            registries.append(entry)

        project.set(
            f"services/{service_name}/config",
            {**(config if isinstance(config, dict) else {}), "registries": registries},
        )
        return created

    async def upsert_registry_by_secret(
        self,
        name: str,
        url: str,
        secret_name: str,
    ) -> dict[str, Any]:
        """Add or update a registry that references a pre-existing Kubernetes secret.

        ``url`` carries no protocol and may include a path; ``secret_name`` must name an
        existing ``kubernetes.io/dockerconfigjson`` secret, which nothing here verifies.
        """
        project_data = await self._host.get_contents()
        project_name = await self._host.get_name()

        registry_entry = {"name": name, "upstream": url, "secretName": secret_name}
        created = self._upsert_registry_entry(project_data, registry_entry)

        action = "Add" if created else "Update"
        await self._host.save_and_commit_project(
            project_data, f"{action} registry '{name}' (secretName) in project '{project_name}'"
        )

        logger.info(f"Successfully {'added' if created else 'updated'} registry '{name}' in project '{project_name}'")
        return {"success": True, "created": created, "registry": registry_entry}

    async def upsert_registry_by_credentials(
        self,
        name: str,
        url: str,
        username: str | None,
        password: str,
    ) -> dict[str, Any]:
        """Add or update a registry with username/password credentials.

        The password is AGE-encrypted with the project's public key before it is stored,
        and never travels back in the result. An empty ``username`` is left out of the
        entry entirely, so the project file carries only what the caller supplied (RC-187).
        """
        project_data = await self._host.get_contents()
        project_name = await self._host.get_name()

        public_key = get_project_public_key(project_data)
        if not public_key:
            return {
                "success": False,
                "error": "Project has no AGE public key configured",
                "error_type": "missing_public_key",
            }

        encrypted_password = LiteralScalarString(await encrypt_age_content(password, public_key))

        registry_entry: dict[str, Any] = {"name": name, "upstream": url, "password": encrypted_password}
        if username:
            registry_entry["username"] = username
        created = self._upsert_registry_entry(project_data, registry_entry)

        action = "Add" if created else "Update"
        await self._host.save_and_commit_project(
            project_data, f"{action} registry '{name}' (credentials) in project '{project_name}'"
        )

        logger.info(f"Successfully {'added' if created else 'updated'} registry '{name}' in project '{project_name}'")
        return {"success": True, "created": created, "registry": {"name": name, "upstream": url, "username": username}}
