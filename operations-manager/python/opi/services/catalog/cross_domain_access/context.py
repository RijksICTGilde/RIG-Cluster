"""Form context for the cross-domain-access selects (RC-42).

One function, used by BOTH form flows: the create wizard (``router_wizard``) and the modal
edit flow (``router_detail_edit``). It used to live in the edit router only, which is why the
peer-project select was populated when editing an existing project and empty -- with three
required fields you could not fill -- in the create wizard.

Only the peer-project list is precomputed, because it is the one thing the form cannot derive
from the data it already holds: it needs the logged-in user and the project store. Everything
else in the cascade (peer deployments, peer components, ports) is a function of a row's own
values and is read at render time by the providers, so it cannot go stale or differ per flow.

Template-only: no editable names ``_cross_domain_projects`` or ``_cross_domain_project_labels``,
so they fall outside the write set and never reach the saved project file.
"""

from __future__ import annotations

from typing import Any

from opi.services.project_authorization import is_user_authorized_for_project
from opi.services.project_store import get_project_store

CROSS_DOMAIN_PROJECTS_KEY = "_cross_domain_projects"
CROSS_DOMAIN_PROJECT_LABELS_KEY = "_cross_domain_project_labels"


def build_cross_domain_context(user_email: str) -> dict[str, Any]:
    """Peer projects this user may point a cross-domain rule at, own project included.

    Limited to projects the user is authorized for: a peer you cannot see is a peer you
    cannot name.

    The own project is in the list on purpose. The tenant baseline isolates per DEPLOYMENT,
    not per project, so one deployment of a project cannot reach another deployment of that
    same project without a rule either -- excluding the own project left that case with no
    way to express it at all.

    Sorted on the display name (case-insensitively), because that is what the select shows;
    the code is only the tiebreaker.
    """
    labels: dict[str, str] = {}
    display_names: dict[str, str] = {}
    for summary in get_project_store().get_all():
        if not is_user_authorized_for_project(summary.name, user_email):
            continue
        display_name = str((summary.data or {}).get("display-name") or "").strip()
        display_names[summary.name] = display_name or summary.name
        # Geen weergavenaam? Dan alleen de code, geen lege haakjes erachter.
        labels[summary.name] = f"{display_name} ({summary.name})" if display_name else summary.name
    projects = sorted(display_names, key=lambda name: (display_names[name].casefold(), name))
    return {CROSS_DOMAIN_PROJECTS_KEY: projects, CROSS_DOMAIN_PROJECT_LABELS_KEY: labels}
