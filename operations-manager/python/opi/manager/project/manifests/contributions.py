"""Which services contribute to a component's template context, and how that merges."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from opi.services.registry import manifest_services

if TYPE_CHECKING:
    from opi.services.catalog.base import ManifestContext, ManifestContribution


def collect_manifest_contributions(
    ctx: ManifestContext, *, component_services: list[str], project_services: list[str]
) -> list[ManifestContribution]:
    """Every contributing service's ``ManifestContribution`` for one component.

    Which services are asked depends on where their selection lives. Most read the
    COMPONENT's own ``services`` list -- each component decides whether it sits behind
    login, gets database credentials, is scraped. A service that declares
    ``selectable_per_component=False`` has no per-component choice to make and reads the
    PROJECT's list instead; no component ever ticks such a service, so a
    component-scoped question would answer "no" for every component forever.

    Module-level so both halves of that rule can be measured without building a whole
    deployment first.
    """
    return [
        provider.contribute_manifest_context(ctx)
        for provider in manifest_services()
        if any(
            service_type.value
            in (component_services if provider.definition.selectable_per_component else project_services)
            for service_type in provider.manifest_activation_types()
        )
    ]


def apply_manifest_contributions(variables: dict[str, Any], contributions: list[ManifestContribution]) -> None:
    """Merge the contributions into a component's template context, in ``manifest_order``.

    Three merge semantics, and the difference is load-bearing:

    * ``template_vars`` OVERRIDE a base key (auth-wall moves ``service_port`` 8080 ->
      4180).
    * ``env_vars`` are ADDITIVE: they join the component's own variables instead of
      replacing them. That is why they are a field of their own rather than a
      ``template_vars`` entry -- as an override, one service handing out one variable
      would wipe every variable the component itself declared.
    * ``sidecars`` are additive too.
    """
    for contribution in contributions:
        variables.update(contribution.template_vars)
        if contribution.env_vars:
            variables["env_vars"] = {**variables.get("env_vars", {}), **contribution.env_vars}
        if contribution.sidecars:
            variables.setdefault("sidecars", []).extend(contribution.sidecars)
