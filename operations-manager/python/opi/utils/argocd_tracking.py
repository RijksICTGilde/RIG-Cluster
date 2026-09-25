"""Which ArgoCD Application owns a live resource.

ArgoCD marks everything it applies, but HOW depends on its ``resourceTrackingMethod``,
and this platform runs both: ``odcn-production`` sets ``annotation``, so resources carry
``argocd.argoproj.io/tracking-id``; ``local`` and ``sandboxed-local`` set nothing and get
ArgoCD's default, ``label``, so resources carry ``app.kubernetes.io/instance`` instead
and no annotation at all. Measured on the sandbox cluster on 24 September 2026: not one
resource under a live Application had a tracking-id.

Reading only the annotation would therefore find nothing on two of the three cluster
types, and a force that finds nothing deletes nothing while reporting success, which is
the damage this is meant to prevent. So both signals count. Neither is ambiguous here:
OPI's own manifests write neither, so whichever is present was written by ArgoCD.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: Annotation ArgoCD writes with resourceTrackingMethod ``annotation``.
TRACKING_ID_ANNOTATION = "argocd.argoproj.io/tracking-id"

#: Label ArgoCD writes with resourceTrackingMethod ``label`` (its default).
INSTANCE_LABEL = "app.kubernetes.io/instance"

#: What Kubernetes allows in a label value. An Application name may be longer: the schema
#: allows 30 for the project plus 63 for the deployment (``project_v2.json``) and
#: ``generate_argocd_application_name`` only caps at 253.
LABEL_VALUE_MAX = 63


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
    """One live resource, and the Application ArgoCD says it belongs to."""

    kind: str
    api_version: str
    name: str
    namespace: str
    app_name: str
    from_label: bool

    @property
    def truncatable(self) -> bool:
        """Whether ``app_name`` may be a longer Application name cut to the label cap.

        Only the label has that cap, so only a name read from one can be a cut one. On
        ``odcn-production``, the one cluster type that sets ``resourceTrackingMethod:
        annotation``, this is never true and every comparison on the name is plain equality.
        """
        return self.from_label and len(self.app_name) == LABEL_VALUE_MAX

    @property
    def kubectl_type(self) -> str:
        """``<kind>.<group>``, the unambiguous form kubectl accepts for a delete."""
        group = self.api_version.rsplit("/", 1)[0] if "/" in self.api_version else ""
        kind = self.kind.lower()
        return f"{kind}.{group}" if group else kind


def tracked_resource_from_item(item: dict[str, Any]) -> TrackedResource | None:
    """Read one ``kubectl get -o json`` item, or ``None`` when ArgoCD does not own it."""
    metadata = item.get("metadata") or {}
    name = metadata.get("name")
    if not name:
        return None

    tracking_id = (metadata.get("annotations") or {}).get(TRACKING_ID_ANNOTATION)
    from_label = not tracking_id
    if tracking_id:
        app_name = application_name_from_tracking_id(tracking_id)
    else:
        # The label method. Taken only in the annotation's absence, so a resource ArgoCD
        # tracks by annotation is never claimed by a label another tool left behind.
        app_name = ((metadata.get("labels") or {}).get(INSTANCE_LABEL) or "").strip() or None

    if not app_name:
        return None

    return TrackedResource(
        kind=item.get("kind", ""),
        api_version=item.get("apiVersion", ""),
        name=name,
        namespace=metadata.get("namespace", ""),
        app_name=app_name,
        from_label=from_label,
    )


def may_be_cut_from(resource: TrackedResource, application_names: set[str]) -> bool:
    """Whether the mark is ambiguous: it may be one of these names cut to the label cap.

    True means UNDECIDABLE, not owned: a value sitting exactly on the cap is equally well the
    whole name of another Application, and nothing in the mark says which of the two it is.
    The conclusion is the caller's, because the two callers draw the opposite one: for the
    sweep a match means leaving a resource alone, for the force it means deleting it. One
    predicate that answered "belongs to" therefore flipped meaning between them, and the
    relaxation that protects a live resource on the sweep took a living neighbour's resources
    on the force (RC-226, review round 9). Neither caller may act destructively on a true;
    each keeps its own further condition beside it.

    Equality does not settle it. A value on the cap is the whole name of one Application AND
    the cut form of every longer name starting with it, and both can be running at the same
    time: on a label cluster every resource of neighbour ``<value>-x`` carries ``<value>``.
    Answering False there let the force delete a living neighbour's PVC and report success
    (RC-226, review round 11). So the only name that cannot make a mark ambiguous is the
    mark itself.
    """
    if not resource.truncatable:
        return False
    return any(name != resource.app_name and name.startswith(resource.app_name) for name in application_names)
