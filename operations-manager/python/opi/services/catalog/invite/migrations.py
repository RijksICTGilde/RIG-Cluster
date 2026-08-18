"""Forward-only config migrations for the invite service.

v1.0 -> v1.1 (RC-136)
---------------------
v1.0 stored the destination of the success button as a finished address
(``application-url``). v1.1 adds ``application-target``: the deployment/component choice
the address is derived from, so the button follows a subdomain or domain-format change
instead of pointing at a hostname that no longer exists.

The step tries to match each stored address against the project's derived addresses.
Matched, it becomes a target and the address is dropped -- the answer is replaced by the
question it was the answer to. NOT matched, the address stays exactly as it is: that is
either a destination outside this project or one that has already gone stale, and
throwing it away silently is worse than keeping something a human can still read.

So the step is lossless in the direction that matters and, because it only ever consumes
``application-url`` and only when it can name what it points at, running it twice changes
nothing the second time.

Deriving the addresses needs the project around the config, which a config block does not
carry. It arrives through the validation context (``PROJECT_DATA_CONTEXT_KEY``); without
it there is nothing to match against and the config is returned untouched.
"""

from __future__ import annotations

import logging
from typing import Any

from opi.services.catalog.invite.destination import target_for_url

logger = logging.getLogger(__name__)

#: Both spellings of the address key. The on-disk config keys are hyphenated, but the
#: files that predate the invite SERVICE carry the underscore field names verbatim (see
#: the config-model docstring), and both validate.
_URL_KEYS = ("application-url", "application_url")
_TARGET_KEYS = ("application-target", "application_target")


def _entry_get(entry: dict[str, Any], keys: tuple[str, ...]) -> tuple[str | None, Any]:
    """The first of ``keys`` present on the entry, with its value."""
    for key in keys:
        if key in entry:
            return key, entry[key]
    return None, None


def migrate_invite_config_1_0_to_1_1(config: Any, project_data: dict[str, Any] | None) -> Any:
    """Replace every derivable ``application-url`` with the ``application-target`` behind it.

    Mutates nothing the caller handed in: the entries that change are replaced by copies.
    """
    if not isinstance(config, dict) or not project_data:
        return config
    active = config.get("active")
    if not isinstance(active, list):
        return config

    migrated: list[Any] = []
    changed = False
    for entry in active:
        if not isinstance(entry, dict):
            migrated.append(entry)
            continue
        url_key, url = _entry_get(entry, _URL_KEYS)
        target_key, _target = _entry_get(entry, _TARGET_KEYS)
        if url_key is None or not isinstance(url, str) or not url or target_key is not None:
            migrated.append(entry)
            continue
        target = target_for_url(url, project_data)
        if target is None:
            logger.debug("Invite destination %s is not one of this project's addresses; keeping it as-is", url)
            migrated.append(entry)
            continue
        #: The hyphen spelling for the new key even where the old one used underscores:
        #: that is what every other key written since the invite service uses, and both
        #: validate, so there is no reason to carry the legacy spelling forward.
        replacement = {key: value for key, value in entry.items() if key != url_key}
        replacement["application-target"] = target
        migrated.append(replacement)
        changed = True

    if not changed:
        return config
    return {**config, "active": migrated}
