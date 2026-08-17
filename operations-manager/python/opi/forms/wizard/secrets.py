"""Keep encrypted values out of the disk-backed wizard session, without a field list.

The modal wizard puts a copy of the project file in its state (``base_data`` plus
the per-section ``step_data``) and serializes that to JSON under ``TEMP_DIR``. Every
encrypted value in the project therefore landed on disk, for every edit, no matter how
small: editing only the authorization-wall banner wrote the keycloak realm admin
password, the api key and the attachment blocks along with it. Worse, the untouched
values then travelled back out of that JSON copy into the saved file, which is how a
realm password lost its literal-block formatting.

Naming the offending fields one by one does not hold: the next encrypted field is added
without anyone remembering this file. So the rule here is default-deny and derived from
the flow itself -- an encrypted value is kept only when some editable in the flow can
actually reach it, and is redacted otherwise. Add an editable and its value is kept
automatically; add an encrypted field with no editable and it is dropped automatically.

What counts as encrypted comes from ``carries_encrypted_value``, which knows both stored
forms the schema declares. Testing for the armored block alone was not enough: the
repository password, the api key and the project private key are stored with the
``base64+age:`` prefix, so they were left in the session file untouched.

The pair is:

* :func:`redact_unreachable_secrets` when the wizard state is built, and
* :func:`restore_redacted_secrets` at save, which puts the values back from the project
  as freshly read from git -- not from the session -- so an untouched secret is written
  back exactly as it was stored.

The one deliberate exception is ``config/age-private-key``. It is not user-editable, but
the converters decrypt and re-encrypt field values with it (``resolve_project_private_key``),
so removing it would break editing every encrypted field.
"""

from __future__ import annotations

import copy
import logging
from typing import Any

from opi.utils.age import carries_encrypted_value

logger = logging.getLogger(__name__)

REDACTED = "__opi-redacted-secret__"
"""Placeholder left behind, so the save side can tell "untouched" from "cleared"."""

_ALWAYS_KEEP = frozenset({"age-private-key"})
"""Leaf keys kept regardless of the flow; see the module docstring."""

_IDENTIFIER_KEYS = ("name", "reference", "realm", "id")
"""Stable identifiers used to pair list items across the two structures. Index position
is the fallback, but a list the form reordered would misalign, and restoring one realm's
password onto another realm is exactly the kind of silent damage worth avoiding."""


def _leaf_key(yaml_path: str) -> str:
    """Last path segment of an editable's yaml_path, without list/filter syntax.

    ``deployments[0]/components{reference=api}/user-env-vars`` -> ``user-env-vars``.
    """
    segment = yaml_path.rsplit("/", 1)[-1]
    for delimiter in ("[", "{"):
        segment = segment.split(delimiter, 1)[0]
    return segment


def reachable_leaf_keys(editables: Any) -> set[str]:
    """Leaf keys every editable in a flow can address, children included.

    Matching on the leaf key rather than the full path is deliberately coarse: paths in
    the state carry concrete list indices while an editable's path may carry a filter or
    none at all, so aligning them exactly would be its own source of bugs. Coarse here
    errs toward keeping a value, which is the harmless direction -- the strict direction
    would silently drop a field the form needs.
    """
    keys: set[str] = set()
    for visualizer in editables or []:
        editable = getattr(visualizer, "editable", None)
        if editable is not None and getattr(editable, "yaml_path", None):
            keys.add(_leaf_key(editable.yaml_path))
        children = getattr(visualizer, "children", None)
        if children:
            keys |= reachable_leaf_keys(children)
    return keys


def redact_unreachable_secrets(data: Any, keep_keys: set[str]) -> tuple[Any, list[str]]:
    """Return a copy of *data* with out-of-reach encrypted values replaced by REDACTED.

    Returns ``(redacted_copy, paths)``; *paths* names what was removed, so a caller can
    log it and a test can assert on it.
    """
    removed: list[str] = []
    keep = keep_keys | _ALWAYS_KEEP
    return _redact(copy.deepcopy(data), [], keep, removed), removed


def _redact(value: Any, path: list[str], keep: set[str], removed: list[str]) -> Any:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key) in keep:
                # BEREIKBAAR, en dan de hele inhoud. Er werd hier afgedaald en binnenin
                # opnieuw geoordeeld, op de naam van het onderliggende veld -- en die naam
                # kent geen editable, dus alles eronder werd weggestreept. Zichtbaar bij de
                # aliassen toen die nog PER WAARDE versleuteld werden: onder de bereikbare
                # sleutel "aliases" stonden sleutels als POSTGRES_HOST, en de gebruiker
                # kreeg in het bewerkveld de tekst __opi-redacted-secret__ te zien in plaats
                # van zijn eigen alias. user-env-vars ontsprong de dans omdat dat EEN
                # versleuteld blok is en dus nooit een afdaling werd; sinds RC-106 geldt dat
                # voor aliassen ook.
                #
                # Deze regel blijft staan, en niet alleen omdat aliassen nu een blok zijn:
                # dit is wat de module bedoelt. Bereikbaarheid gaat erover of het formulier
                # de waarde kan terugzetten, en de editable van een map bezit die map in
                # zijn geheel. Grof in de richting van bewaren, zoals reachable_leaf_keys
                # al opschrijft.
                continue
            if carries_encrypted_value(item):
                removed.append("/".join([*path, str(key)]))
                value[key] = REDACTED
            else:
                value[key] = _redact(item, [*path, str(key)], keep, removed)
        return value
    if isinstance(value, list):
        for index, item in enumerate(value):
            value[index] = _redact(item, [*path, str(index)], keep, removed)
        return value
    return value


def restore_redacted_secrets(data: Any, original: Any) -> Any:
    """Return a copy of *data* with every REDACTED placeholder taken from *original*.

    A placeholder with nothing to restore from (a list item the form added) has its key
    dropped rather than written, so the placeholder can never reach the project file.

    Dropping is the safe direction but it is not free: a field that is required shows up
    as a refused save, and a field that is optional (``totp_secret`` next to the realm
    password) simply disappears from the project file with nobody the wiser. So every drop
    is logged with its path -- the drop that is correct (a list item the form just added)
    and the drop that is a pairing bug read the same here, and only the log tells them
    apart afterwards.
    """
    return _restore(data, original, [])


def _restore(data: Any, original: Any, path: list[str]) -> Any:
    if isinstance(data, dict):
        source = original if isinstance(original, dict) else {}
        restored: dict[Any, Any] = {}
        for key, item in data.items():
            if item == REDACTED:
                if source.get(key) is not None:
                    restored[key] = source[key]
                else:
                    logger.warning(
                        "Redacted secret at %s has no stored counterpart; dropping the key",
                        "/".join([*path, str(key)]),
                    )
                continue
            restored[key] = _restore(item, source.get(key), [*path, str(key)])
        return restored
    if isinstance(data, list):
        source_list = original if isinstance(original, list) else []
        return [
            _restore(item, _pair_with(item, source_list, index), [*path, str(index)])
            for index, item in enumerate(data)
            if item != REDACTED
        ]
    return data


def _pair_with(item: Any, source: list[Any], index: int) -> Any:
    """The element of *source* that corresponds to *item*: by identifier, else position."""
    if isinstance(item, dict):
        for key in _IDENTIFIER_KEYS:
            identifier = item.get(key)
            if identifier is None:
                continue
            match = next(
                (other for other in source if isinstance(other, dict) and other.get(key) == identifier),
                None,
            )
            if match is not None:
                return match
    return source[index] if index < len(source) else None
