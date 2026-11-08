"""What a write has to tell its caller: ungranted approvals, warnings, advice."""

from __future__ import annotations

from typing import Any

from opi.core.config import settings
from opi.services.approvals import collect_deployment_approval_notices
from opi.services.catalog.publish_on_web.domain_config import (
    DomainSetting,
    custom_domain_certificate_note,
    get_domain_setting,
)


def approval_notices(project_data: dict[str, Any], deployment_name: str | None) -> list[dict[str, Any]]:
    """What the owner of ``deployment_name`` must be told about ungranted approvals.

    Empty without a deployment name: everything that needs approving is deployment-level.
    """
    if not deployment_name:
        return []
    deployment = next(
        (d for d in project_data.get("deployments", []) if isinstance(d, dict) and d.get("name") == deployment_name),
        None,
    )
    if deployment is None:
        return []
    return list(collect_deployment_approval_notices(project_data, deployment))


def certificate_warnings(project_data: dict[str, Any], deployment_name: str) -> list[str]:
    """What this write means for the deployment's certificate, if anything.

    Reported where the approval notice stops: an own domain on a cluster that cannot issue
    for it is no longer reported once an approver grants it, which is exactly when the
    certificate never appears (zad-cli, bevinding 22).
    """
    deployment = next(
        (d for d in project_data.get("deployments", []) if isinstance(d, dict) and d.get("name") == deployment_name),
        None,
    )
    if deployment is None:
        return []
    note = custom_domain_certificate_note(
        settings.CLUSTER_MANAGER, get_domain_setting(deployment, DomainSetting.BASE_DOMAIN)
    )
    return [note] if note else []


def config_advice_warnings(project_data: dict[str, Any]) -> list[str]:
    """What this project now expects but does not have, after this write.

    Judges the whole project, not only the block that was just written. Each message is
    prefixed with its field path, which is what distinguishes one entry of a list from
    the next.
    """
    from opi.services.services import collect_config_advice

    return [f"{notice.field_path}: {notice.message}" for notice in collect_config_advice(project_data)]
