"""Writing the project's attachment catalog and the couplings to it (RC-38, RC-52)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ruamel.yaml.scalarstring import LiteralScalarString

from opi.core.project_schema import ProjectIntegrityError, ProjectSchemaError
from opi.handlers.project_file_handler import (
    USAGE_CERTIFICATE,
    attachment_usage_sites,
    ensure_attachment_data_list,
    extract_attachment_catalog,
    extract_component_attachment_uses,
    find_attachment_data_list,
    remove_attachment_references,
)
from opi.services import ServiceAdapter, ServiceType
from opi.services.catalog.base import ConfigLayer
from opi.utils.age import encrypt_file_to_age_block_sync

if TYPE_CHECKING:
    from opi.manager.project.access import ProjectFileAccess

logger = logging.getLogger(__name__)


class AttachmentMutations:
    """The attachment writers of ``ProjectManager``, built per call around its host."""

    def __init__(self, host: ProjectFileAccess) -> None:
        self._host = host

    async def remove_attachment(self, attachment_id: str, *, confirm_in_use: bool = False) -> dict[str, Any]:
        """Remove an attachment from the project's catalog.

        Idempotent (already-absent is a no-op success) and, by default, refuses removal
        while the attachment is still referenced, with ``used_by`` naming every place that
        references it.

        ``confirm_in_use`` drops every component and deployment-component coupling along
        with the catalog entry, in a single save: a reference pointing at a deleted id
        makes the project file invalid.

        Refused even with the confirmation: an attachment serving as a publish-on-web
        certificate (``tls: provided``). Removing that reference means deciding how the
        site should be served instead.
        """
        try:
            project_data = await self._host.get_contents()
            project_name = await self._host.get_name()

            data = find_attachment_data_list(project_data.get("services"))
            if data is None or not any(isinstance(e, dict) and e.get("id") == attachment_id for e in data):
                return {"success": True, "changed": False}

            sites = attachment_usage_sites(project_data).get(attachment_id, [])
            certificates = [site for site in sites if site.kind == USAGE_CERTIFICATE]
            if sites and (not confirm_in_use or certificates):
                if certificates:
                    reason = (
                        f"Bijlage '{attachment_id}' wordt als certificaat gebruikt door: "
                        f"{', '.join(site.label for site in certificates)}. Wijzig eerst de TLS-modus daar; "
                        "een certificaat kan niet zomaar worden losgekoppeld."
                    )
                else:
                    reason = (
                        f"Bijlage '{attachment_id}' is in gebruik door: "
                        f"{', '.join(site.label for site in sites)} en kan niet worden verwijderd"
                    )
                return {
                    "success": False,
                    "error": reason,
                    "error_type": "in_use",
                    "used_by": [site.as_dict() for site in sites],
                }

            uncoupled = remove_attachment_references(project_data, attachment_id) if sites else []
            data[:] = [e for e in data if not (isinstance(e, dict) and e.get("id") == attachment_id)]

            where = f" and uncoupled from {', '.join(site.label for site in uncoupled)}" if uncoupled else ""
            await self._host.save_and_commit_project(
                project_data, f"Remove attachment '{attachment_id}' from project '{project_name}'{where}"
            )

            logger.info(f"Removed attachment '{attachment_id}' from project '{project_name}'{where}")
            return {"success": True, "changed": True, "uncoupled_from": [site.as_dict() for site in uncoupled]}

        except (ProjectSchemaError, ProjectIntegrityError) as e:
            # The save is the check that a cleanup left no reference behind: a dangling
            # reference is exactly what validate_attachment_references rejects here.
            logger.warning("Attachment '%s' removal rejected by project validation: %s", attachment_id, e)
            return {"success": False, "error": str(e), "error_type": "validation_error"}
        except Exception as e:
            error_msg = f"Error removing attachment '{attachment_id}': {e}"
            logger.exception(error_msg)
            return {"success": False, "error": "An internal error occurred", "error_type": "internal_error"}

    async def upsert_attachment(
        self,
        attachment_id: str,
        filename: str | None,
        content: bytes | None,
        *,
        on_existing: str,
        on_absent: str,
        component_name: str | None = None,
        binding: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Put one attachment in the project's catalog, optionally bound to a component.

        The write side of the attachments service's DEFINE role: the file is encrypted
        with the project's AGE public key and stored in the catalog under
        ``attachment_id``. With ``component_name`` and ``binding`` the same call also
        records the component's *use* of it (which attachment) and its *binding* (as a
        file at a path, or as an env-var), so upload and couple is one request.

        ``on_existing`` / ``on_absent`` carry the verb's promise (see
        ``services/catalog/actions.py``): ``reject`` refuses, ``replace`` overwrites
        without asking, ``create`` adds.

        Without ``content`` the caller referenced an attachment that is already in the
        catalog: nothing is encrypted and the catalog entry is left untouched, only the
        component's use of it is written. The verb's promise then applies to that
        coupling, and an absent catalog entry is a refusal.
        """
        try:
            project_data = await self._host.get_contents()
            project_name = await self._host.get_name()

            catalog = extract_attachment_catalog(project_data)
            exists = attachment_id in catalog
            if content is None:
                # Reference only: the entry has to be there.
                if not exists:
                    return {
                        "success": False,
                        "error": f"Bijlage '{attachment_id}' bestaat niet in project '{project_name}'",
                        "error_type": "not_found",
                    }
            elif exists and on_existing == "reject":
                return {
                    "success": False,
                    "error": f"Bijlage '{attachment_id}' bestaat al in project '{project_name}'",
                    "error_type": "conflict",
                }
            elif not exists and on_absent == "reject":
                return {
                    "success": False,
                    "error": f"Bijlage '{attachment_id}' bestaat niet in project '{project_name}'",
                    "error_type": "not_found",
                }

            public_key = (project_data.get("config") or {}).get("age-public-key")
            if content is not None and not public_key:
                # Fail closed rather than store the bytes unencrypted: the catalog is
                # committed to the projects repository, and attachments are certificates.
                return {
                    "success": False,
                    "error": f"Project '{project_name}' heeft geen AGE-publieke sleutel",
                    "error_type": "no_encryption_key",
                }

            if component_name and not any(
                isinstance(c, dict) and c.get("name") == component_name
                for c in project_data.get("components", []) or []
            ):
                return {
                    "success": False,
                    "error": f"Component '{component_name}' bestaat niet in project '{project_name}'",
                    "error_type": "not_found",
                }

            # The service must be selected on the project (and on the component, when one
            # is named) or the reference has nothing to resolve against.
            ServiceAdapter.add_services_to_project(
                project_data, [ServiceType.ATTACHMENTS.value], [component_name] if component_name else None
            )

            if content is not None:
                data_list = ensure_attachment_data_list(project_data.setdefault("services", []))
                entry = {
                    "id": attachment_id,
                    "filename": filename,
                    "content": LiteralScalarString(encrypt_file_to_age_block_sync(content, public_key)),
                }
                for index, existing in enumerate(data_list):
                    if isinstance(existing, dict) and existing.get("id") == attachment_id:
                        data_list[index] = entry
                        break
                else:
                    data_list.append(entry)

            coupled = False
            if component_name and binding:
                component = next(
                    c for c in project_data["components"] if isinstance(c, dict) and c.get("name") == component_name
                )
                uses = [
                    use for use in extract_component_attachment_uses(component) if use.get("reference") != attachment_id
                ]
                coupled = len(uses) != len(extract_component_attachment_uses(component))
                if content is None:
                    # Nothing is written yet, so refusing here leaves the project as it was.
                    if coupled and on_existing == "reject":
                        return {
                            "success": False,
                            "error": (
                                f"Component '{component_name}' gebruikt bijlage '{attachment_id}' al in "
                                f"project '{project_name}'"
                            ),
                            "error_type": "conflict",
                        }
                    if not coupled and on_absent == "reject":
                        return {
                            "success": False,
                            "error": (
                                f"Component '{component_name}' gebruikt bijlage '{attachment_id}' niet in "
                                f"project '{project_name}'"
                            ),
                            "error_type": "not_found",
                        }
                uses.append({"reference": attachment_id, **binding})
                ServiceAdapter.set_service_config(
                    project_data,
                    ServiceType.ATTACHMENTS.value,
                    ConfigLayer.COMPONENT,
                    uses,
                    component_name=component_name,
                )

            where = f" and coupled to component '{component_name}'" if component_name and binding else ""
            what = "Couple attachment" if content is None else "Upsert attachment"
            try:
                await self._host.save_and_commit_project(
                    project_data, f"{what} '{attachment_id}' in project '{project_name}'{where}"
                )
            except (ProjectSchemaError, ProjectIntegrityError) as e:
                return {"success": False, "error": str(e), "error_type": "validation_error"}

            logger.info(f"{what} '{attachment_id}' in project '{project_name}'{where}")
            return {
                "success": True,
                "attachment": attachment_id,
                "replaced": coupled if content is None else exists,
                "component": component_name,
            }

        except Exception as e:
            error_msg = f"Error upserting attachment '{attachment_id}': {e}"
            logger.exception(error_msg)
            return {"success": False, "error": "An internal error occurred", "error_type": "internal_error"}
