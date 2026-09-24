"""The ``argocd.argoproj.io/tracking-id`` annotation: which Application owns a resource.

ArgoCD stamps every resource it applies with this annotation. It is the only link back
from a live resource to the Application that put it there, so it is what both the delete
route and the sweep use to find what an Application owns.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: Annotation ArgoCD writes on every resource it applies.
TRACKING_ID_ANNOTATION = "argocd.argoproj.io/tracking-id"


def application_name_from_tracking_id(tracking_id: str) -> str | None:
    """The Application name inside a tracking-id, or ``None`` when there is none.

    The value is ``<app>:<group>/<Kind>:<namespace>/<name>``. With app-in-any-namespace
    the app part is ``<app-namespace>/<app>``, so the name is what follows the slash.
    """
    app_part = tracking_id.split(":", 1)[0].strip()
    if not app_part:
        return None
    return app_part.rsplit("/", 1)[-1] or None


@dataclass(frozen=True)
class TrackedResource:
    """One live resource, and the Application its tracking-id points at."""

    kind: str
    api_version: str
    name: str
    namespace: str
    app_name: str
    tracking_id: str
    being_deleted: bool

    @property
    def kubectl_type(self) -> str:
        """``<kind>.<group>``, the unambiguous form kubectl accepts for a delete."""
        group = self.api_version.rsplit("/", 1)[0] if "/" in self.api_version else ""
        kind = self.kind.lower()
        return f"{kind}.{group}" if group else kind


def tracked_resource_from_item(item: dict[str, Any]) -> TrackedResource | None:
    """Read one ``kubectl get -o json`` item, or ``None`` when ArgoCD does not track it."""
    metadata = item.get("metadata") or {}
    tracking_id = (metadata.get("annotations") or {}).get(TRACKING_ID_ANNOTATION)
    if not tracking_id:
        return None

    app_name = application_name_from_tracking_id(tracking_id)
    name = metadata.get("name")
    if not app_name or not name:
        return None

    return TrackedResource(
        kind=item.get("kind", ""),
        api_version=item.get("apiVersion", ""),
        name=name,
        namespace=metadata.get("namespace", ""),
        app_name=app_name,
        tracking_id=tracking_id,
        being_deleted=bool(metadata.get("deletionTimestamp")),
    )
