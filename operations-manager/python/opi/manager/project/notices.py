"""What a write has to tell its caller: ungranted approvals, warnings, advice.

Three pure functions on ``project_data``. They were ``@staticmethod`` on
``ProjectManager``, and they are shared: a deployment write and a service-config write both
report the same things about the same state, so neither mutation group owns them.
"""

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

    Empty when the write named no deployment: the values that need approving are
    deployment-level, so there is nothing to report about a project- or
    component-level write. The sentences come from the services themselves
    (``collect_deployment_approval_notices``), so the API says the same thing the
    portal says about the same state.
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

    A warning and not an approval: ``approvals`` reports what is waiting for an
    administrator, and this waits for nobody. It is also the state that BEGINS where
    the approval notice ends -- a domain of the user's own on a cluster that cannot
    issue for it stops being reported the moment an approver grants it, and that is
    exactly when the certificate never appears (zad-cli, bevinding 22).

    The sentence comes from publish-on-web, so the API says what the wizard's field
    warning says about the same value.
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

    Same channel and same reasoning as ``certificate_warnings``: nobody is waiting
    on an administrator, so this is not an approval, and the project file is valid,
    so it is not an error. It is a field that has become necessary because of a
    setting somewhere else, and the sentence comes from the service that owns the
    field, so the API says what the wizard's field warning says.

    The whole project is judged, not only the block that was just written. The two
    halves of an advice sit in two services, and either write can be the one that
    makes it true -- switching keycloak's ``restrict-access`` on is as much the
    moment as saving a roleless invite is.

    Prefixed with the path so a caller knows which entry of a list is meant; the
    message alone would name the same thing for every invite.
    """
    from opi.services.services import collect_config_advice

    return [f"{notice.field_path}: {notice.message}" for notice in collect_config_advice(project_data)]
