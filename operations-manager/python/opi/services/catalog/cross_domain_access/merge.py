"""Layer merge for cross-domain-access rules (RC-15, pure functions).

The deployment layer is a partial patch on the project layer, keyed on a rule's ``name``.
``merge_rules`` does the structural patch; ``to_merged_rule`` validates one merged rule
against the full model (Step 2) and normalizes inbound and outbound down to one
direction-independent shape (``MergedRule``) so the resolver need not know the direction.

Effective rules for a deployment D = the root rules, each patched by D's same-named rule,
plus D's own new rules, minus the ``disabled`` ones, minus the ones that after the merge
still have no peer deployment.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass

from pydantic import ValidationError

from opi.services.catalog.cross_domain_access.config_model import WILDCARD_PROJECT, InboundRule, OutboundRule


class IncompleteRuleError(ValueError):
    """A merged rule is missing (or carries a bad value in) a required field other than the
    peer deployment. Such a rule should never have existed at the root; it is logged and
    skipped during generation, never crashing the deploy."""


@dataclass(frozen=True)
class MergedRule:
    """A complete, direction-independent cross-domain rule ready to be resolved.

    Inbound and outbound differ only in which side (``from``/``to``) holds the peer; once
    merged and validated they carry exactly these values, so the resolver treats both the
    same.

    A rule with the WILDCARD peer project (inbound only) has no peer at all: ``is_open`` is
    True and the three ``peer_*`` fields are None. Both halves are set together here, from
    a combination only the model can produce, so "no peer" can never be a peer that quietly
    went missing.
    """

    name: str
    peer_project: str | None
    peer_deployment: str | None
    peer_component: str | None
    local_component: str
    port: int
    #: Derived, never stored: nothing in the config is called "open". This is what
    #: ``from: {project: "*"}`` becomes once ``to_merged_rule`` has recognized it, so the
    #: resolver need not know about the wildcard. Named ``is_open`` and not ``open``
    #: because the bare word is a Python builtin and reads like the verb.
    is_open: bool = False


def _patch(base: dict, patch: dict) -> None:
    """Overlay ``patch`` onto ``base`` in place, one level deep.

    Only present, non-None keys override, so a patch that sets just ``to.deployment`` never
    wipes an inherited ``component`` or ``port``. ``from``/``to`` are merged field-by-field;
    every other key is replaced wholesale.
    """
    for key, value in patch.items():
        if value is None:
            continue
        if key in ("from", "to") and isinstance(value, dict) and isinstance(base.get(key), dict):
            for sub_key, sub_value in value.items():
                if sub_value is not None:
                    base[key][sub_key] = sub_value
        else:
            base[key] = value


def merge_rules(project_rules: list[dict], deployment_rules: list[dict]) -> list[dict]:
    """Merge the deployment layer onto the project layer, keyed on ``name`` (pure).

    A deployment rule whose ``name`` exists at root patches it field-by-field; a new name is
    appended whole. ``disabled: true`` (from either layer, after the patch) drops the rule.
    Result order: root rules in their own order, then the new deployment-only rules.
    """
    merged: dict[str, dict] = {}
    order: list[str] = []
    for rule in project_rules:
        name = rule.get("name")
        if name is None:
            continue
        merged[name] = copy.deepcopy(rule)
        order.append(name)
    for rule in deployment_rules:
        name = rule.get("name")
        if name is None:
            continue
        if name in merged:
            _patch(merged[name], rule)
        else:
            merged[name] = copy.deepcopy(rule)
            order.append(name)

    result: list[dict] = []
    for name in order:
        rule = merged[name]
        if rule.get("disabled"):
            continue
        rule.pop("disabled", None)  # not part of a resolved rule
        result.append(rule)
    return result


def to_merged_rule(rule: dict, *, direction: str) -> MergedRule | None:
    """Validate one merged rule against the full model and normalize it (pure).

    ``direction`` is ``"inbound"`` or ``"outbound"`` and selects which side is the peer.
    Returns ``None`` when the rule is complete except for the peer deployment -- the root may
    leave that open and the deployment layer fill it; a rule that stays open is skipped by
    the caller with a UI-visible notice. Raises ``IncompleteRuleError`` when any other
    required field is missing or invalid.

    A WILDCARD inbound rule (``from.project == "*"``) has no peer, so it returns immediately
    and is never subject to the peer-deployment check. Mind the two meanings of "open" in
    this module: a rule with an OPEN PEER DEPLOYMENT is incomplete and waits for a patch,
    a wildcard rule is complete and deliberately has no peer.
    """
    model = InboundRule if direction == "inbound" else OutboundRule
    try:
        validated = model.model_validate(rule)
    except ValidationError as error:
        name = rule.get("name", "(zonder naam)")
        problems = "; ".join(f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in error.errors())
        raise IncompleteRuleError(
            f"cross-domain {direction}-regel '{name}' is onvolledig of ongeldig: {problems}"
        ) from error

    if isinstance(validated, InboundRule):
        if validated.from_.project == WILDCARD_PROJECT:
            # No peer to wait for, so a wildcard rule is never held back by the
            # peer-deployment check below. The model already refused a wildcard that
            # also named a deployment or component.
            return MergedRule(
                name=validated.name,
                peer_project=None,
                peer_deployment=None,
                peer_component=None,
                local_component=validated.to.component,
                port=validated.to.port,
                is_open=True,
            )
        peer = validated.from_
        local_component = validated.to.component
        port = validated.to.port
    else:
        peer = validated.to
        local_component = validated.from_.component
        port = validated.to.port

    if peer.deployment is None:
        return None
    assert peer.component is not None  # noqa: S101 - only a wildcard peer may omit it, handled above
    return MergedRule(
        name=validated.name,
        peer_project=peer.project,
        peer_deployment=peer.deployment,
        peer_component=peer.component,
        local_component=local_component,
        port=port,
    )
