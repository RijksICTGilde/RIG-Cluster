"""Form converters for the invite service.

The destination picker shows addresses because that is what a person recognises, and
stores the deployment/component CHOICE because that is what survives a subdomain or
domain-format change. This converter is the hinge between the two: address on the form
side, target in the project file.

The options themselves are unchanged (``InviteApplicationUrlOptionsProvider``): the same
list, the same labels, the same path suffix where a component publishes more than one
address. Only what a save writes down changed.
"""

from __future__ import annotations

from typing import Any

from opi.services.catalog.invite.destination import target_for_url, url_for_target


class ApplicationTargetConverter:
    """Address (form) <-> ``{deployment, component, path}`` (project file).

    An address that is not one of this project's own yields no target, so nothing is
    written. That covers the empty option ("no button") and an option that has since
    stopped being derivable; both mean the same thing to the file, which is that there is
    no choice here to record. A fixed ``application-url`` for a destination outside the
    project is a separate field and not this picker's business.
    """

    def read(self, value: Any, context_data: dict[str, Any] | None = None) -> str:
        """Stored target -> the address to preselect in the list.

        A target that no longer resolves preselects nothing, which is the same thing the
        success page does with it: show no button. The list then reads as "no
        destination", and it is -- the stored one has stopped pointing anywhere.
        """
        if not isinstance(value, dict) or not context_data:
            return ""
        return url_for_target(value, context_data) or ""

    def write(self, value: Any, context_data: dict[str, Any] | None = None) -> dict[str, str] | None:
        """Picked address -> the target behind it, or None to write nothing."""
        if not value or not isinstance(value, str) or not context_data:
            return None
        return target_for_url(value, context_data)

    def view(self, value: Any, context_data: dict[str, Any] | None = None) -> str:
        """Read-only display: the address it points at today, or a plain statement."""
        if not isinstance(value, dict):
            return "Geen knop tonen"
        if not context_data:
            return f"{value.get('deployment', '?')} / {value.get('component', '?')}"
        return url_for_target(value, context_data) or "Bestemming bestaat niet meer"
