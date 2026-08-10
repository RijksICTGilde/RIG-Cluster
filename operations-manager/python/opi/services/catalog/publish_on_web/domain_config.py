"""Where a deployment's web-address settings live, and the only way to reach them (RC-60).

The seven fields that together describe how publish-on-web composes a hostname and with
which certificate used to sit loose in the deployment root::

    deployments:
      - name: productie
        domain-format: component-deployment-project
        base-domain: rijksapp.nl

They now live under the service that owns them, at the layer where the value applies::

    deployments:
      - name: productie
        services:
          - reference: publish-on-web
            config:
              domain-format: component-deployment-project
              base-domain: rijksapp.nl

This module is the single authority on that location -- the same role
``connectors/subdomain.py`` plays for the project-level ``domains`` block, and for the same
reason. The v2.6 -> v2.7 migration relocates through :func:`relocate_domain_settings` and the
runtime reads through :func:`get_domain_setting`, so the migration and the running code can
never disagree about where a value is.

Readers accept BOTH locations: service config first, deployment root as fallback. A file that
has not been migrated yet keeps working and relocates on its next load. Writers only ever use
:func:`ensure_domain_config` / :func:`set_domain_setting`, which land on the service path and
remove the root copy, so the state cannot split.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from opi.services.catalog.base import ConfigLayer, config_path
from opi.services.services import service_entry_body, service_entry_name
from opi.services.services_enums import ServiceType


class DomainSetting(StrEnum):
    """The seven deployment-level settings publish-on-web owns.

    An enum rather than seven string literals so the set is enumerable (the migration and
    the "no direct root access" guard both need to walk it) and every reader names the same
    key. The values are the on-disk key spelling, unchanged by the relocation.
    """

    BASE_DOMAIN = "base-domain"
    SUBDOMAIN = "subdomain"
    DOMAIN_MODE = "domain-mode"
    DOMAIN_FORMAT = "domain-format"
    ISSUER = "issuer"
    ROOT_COMPONENT = "root-component"
    BARE_DOMAIN_COMPONENT = "expose-component-on-bare-domain"


#: The on-disk keys, in declaration order. Handy for migrations and guards.
DOMAIN_SETTING_KEYS: tuple[str, ...] = tuple(setting.value for setting in DomainSetting)

_SERVICE = ServiceType.PUBLISH_ON_WEB.value

#: The roots a deployment's service config can sit under, real first. A saved project file
#: uses ``services``; mid-wizard the form posts under the virtual ``_services-config`` key so
#: a config section does not collide with the selection list it configures. Readers here must
#: see both, or every enforcer and condition reads None while the user is still filling the
#: form in.
#:
#: Spelled out rather than imported from ``forms.editables.editable.SERVICE_VIRTUALIZE``,
#: which is the one place that DECLARES the pair: importing anything under ``opi.forms``
#: runs ``opi/forms/__init__.py``, which imports the renderer, which reads settings through
#: this module -- a cycle. This module has to stay reachable from connectors and managers, so
#: it keeps its own copy and ``tests/test_publish_on_web_domain_config.py`` locks the two
#: together instead.
_SERVICE_ROOTS: tuple[str, str] = ("services", "_services-config")
#: Where a write lands: the real key. The wizard's own writes go through the editable
#: machinery, which virtualizes on the way out and folds back on the way in.
_WRITE_ROOT = _SERVICE_ROOTS[0]


def _find_entry(deployment: dict[str, Any]) -> tuple[str, Any] | tuple[None, None]:
    """The deployment's publish-on-web service entry and the root it sits under."""
    for root in _SERVICE_ROOTS:
        entries = deployment.get(root)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if service_entry_name(entry) == _SERVICE:
                return root, entry
    return None, None


def get_domain_config(deployment: dict[str, Any]) -> dict[str, Any] | None:
    """The service's deployment-level config block, or None. Never mutates."""
    _root, entry = _find_entry(deployment)
    if entry is None:
        return None
    body = service_entry_body(entry, _SERVICE)
    if not isinstance(body, dict):
        return None
    config = body.get("config")
    return config if isinstance(config, dict) else None


def get_domain_setting(deployment: dict[str, Any], setting: DomainSetting, default: Any = None) -> Any:
    """One web-address setting of ``deployment``: service config first, root as fallback.

    ``default`` is returned when the key is absent from both places. A key that IS present
    wins even when its value is falsy -- a stored ``base-domain: null`` means "no domain of
    its own", which is not the same as "not configured", and collapsing the two would make a
    deployment fall back to the cluster domain on the service path but not on the root path.
    """
    config = get_domain_config(deployment)
    if config is not None and setting.value in config:
        return config[setting.value]
    return deployment.get(setting.value, default)


def get_domain_settings(deployment: dict[str, Any]) -> dict[str, Any]:
    """All seven settings resolved, keyed by their on-disk name (absent ones as None)."""
    return {setting.value: get_domain_setting(deployment, setting) for setting in DomainSetting}


def ensure_domain_config(deployment: dict[str, Any]) -> dict[str, Any]:
    """The writable service ``config`` block of ``deployment``, creating it as needed.

    The single authority on WHERE a web-address setting is written, used by both the v2.7
    migration and every runtime write path. Adds the service entry if it is absent, promotes
    a bare ``- publish-on-web`` string to a ``{reference, config}`` record, and absorbs any
    settings still sitting in the deployment root (root loses to an already-relocated value,
    which is the idempotent choice: a second run finds nothing left to absorb). The root
    copies are removed, so the state never splits.
    """
    root, entry = _find_entry(deployment)
    if root is None:
        root = _WRITE_ROOT
    services = deployment.get(root)
    if not isinstance(services, list):
        services = []
        deployment[root] = services

    if entry is None or isinstance(entry, str):
        record: dict[str, Any] = {"reference": _SERVICE, "config": {}}
        if entry is None:
            services.append(record)
        else:  # bare string reference -> promote in place
            services[services.index(entry)] = record
        entry = record

    body = service_entry_body(entry, _SERVICE)
    if not isinstance(body, dict):
        # A record whose body is not a mapping (e.g. ``{publish-on-web: null}``): replace it
        # with the canonical record rather than writing into something unaddressable.
        body = {"reference": _SERVICE, "config": {}}
        services[services.index(entry)] = body

    config = body.get("config")
    if not isinstance(config, dict):
        config = {}
        body["config"] = config

    for key in DOMAIN_SETTING_KEYS:
        if key in deployment:
            config.setdefault(key, deployment.pop(key))
    return config


def set_domain_setting(deployment: dict[str, Any], setting: DomainSetting, value: Any) -> None:
    """Write one web-address setting onto ``deployment``, at the service path."""
    ensure_domain_config(deployment)[setting.value] = value


def pop_domain_setting(deployment: dict[str, Any], setting: DomainSetting) -> None:
    """Remove one web-address setting from ``deployment``, wherever it is stored.

    Removes from both locations: a caller deleting a setting means the deployment no longer
    has it, and leaving a root copy behind would resurrect the old value on the next read.
    """
    config = get_domain_config(deployment)
    if config is not None:
        config.pop(setting.value, None)
    deployment.pop(setting.value, None)


def clear_domain_settings(deployment: dict[str, Any]) -> None:
    """Remove the whole web address from ``deployment``, wherever it is stored.

    For a deployment that must not carry one at all -- a clone, which uses its own
    (target) domain setup and never the source's hostnames. Excluding the seven root keys
    from a copy is not enough since they moved: they now travel inside the source's
    ``services`` block, which is copied as a whole.

    The service entry itself is dropped when the web address was all it held, so a clone
    does not end up with an empty ``publish-on-web`` record it never asked for.
    """
    for setting in DomainSetting:
        pop_domain_setting(deployment, setting)

    root, entry = _find_entry(deployment)
    if root is None or entry is None:
        return
    body = service_entry_body(entry, _SERVICE)
    if not isinstance(body, dict):
        return  # a bare ``- publish-on-web`` string says "enabled", which is not a web address
    if body.get("config"):
        return
    if any(key not in ("name", "reference", "config") for key in body):
        return  # carries more than the web address (e.g. a schema-version); leave it
    services = deployment[root]
    services.remove(entry)
    if not services:
        del deployment[root]


def relocate_domain_settings(deployment: dict[str, Any]) -> bool:
    """Move any root-level web-address settings of ``deployment`` under the service.

    The migration primitive (v2.6 -> v2.7). Returns True when something moved. Idempotent:
    a deployment with no root-level settings is left completely untouched -- in particular
    no empty service entry is created for a deployment that never had a web address.
    """
    if not any(key in deployment for key in DOMAIN_SETTING_KEYS):
        return False
    ensure_domain_config(deployment)
    return True


def domain_setting_path(setting: DomainSetting, deployment_index: int | None = None) -> str:
    """The form ``yaml_path`` of one web-address setting, optionally for one deployment.

    The forms counterpart of the accessors above, and it lives here for the same reason:
    one statement of where a setting is. The resolver map and several
    ``get_effective_value`` callers key on the exact path an editable declares, and those
    call sites used to spell ``f"deployments[{i}]/base-domain"`` by hand. A hand-written
    path that no longer exists fails silently -- the resolver is simply never found and the
    field falls back to its own default.

    Built from ``config_path``, so the layer prefix comes from ``catalog/base.py`` and this
    cannot drift from what the editables declare.
    """
    path = config_path(ConfigLayer.DEPLOYMENT, ServiceType.PUBLISH_ON_WEB, "config", setting.value)
    if deployment_index is not None:
        path = path.replace("deployments[*]", f"deployments[{deployment_index}]")
    return path
