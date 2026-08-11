"""Reading and writing the key/value properties a component owns (RC-55).

Two system services own a plain property on a component instead of a config block in a
``services:`` list: ``user-env-vars`` and ``aliases`` (see their packages under
``opi/services/catalog/``). Both are a map of environment-variable name -> value, and
both hold secrets, but they are **not stored the same way**:

* ``ValueStorage.BLOCK`` (``user-env-vars``) -- the whole set is one AGE-encrypted block
  whose plaintext is ``KEY=value`` lines. One ciphertext for the set.
* ``ValueStorage.PER_VALUE`` (``aliases``) -- a mapping whose keys stay readable and
  whose values are each AGE-encrypted on their own. One ciphertext per value.

Changing either is always decrypt -> mutate -> re-encrypt; there is no way to edit one
entry of a block in place. This module is that round trip, and it is deliberately
**fail-closed**: without a public key ``encode`` raises instead of writing plaintext,
and a value that announces itself as AGE-encrypted but will not decrypt raises instead
of being carried through as if it were plaintext.

That is the one thing it does NOT share with ``KeyValueConverter``
(``opi/forms/editables/converters.py``), which catches broadly and returns the plain
value with a warning. For a form, where the author is looking at the field and can try
again, that is defensible. For an API write path it would put a secret in git, so this
module does not reuse it. The converter is left alone: it is the wizard's read/write
path and changing its failure mode is a separate change with its own blast radius.

Legacy shapes are read, never written: a ``user-env-vars`` that is still a plain
``KEY=value`` block or a legacy mapping is parsed, and a plaintext alias value is taken
as-is. The first write through here stores the encrypted shape.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any

from ruamel.yaml.scalarstring import LiteralScalarString

from opi.services.catalog.base import ConfigLayer, ValueStorage
from opi.utils.age import (
    decrypt_age_content_sync,
    encrypt_age_content_sync,
    get_decoded_project_private_key_sync,
    get_project_public_key,
    is_age_encrypted,
)
from opi.utils.env_vars import validate_and_parse_env_vars

#: The name a key must have. An alias becomes an environment variable just like an
#: env-var does, so the same rule holds for both -- the same expression the env-var
#: parser and ``AliasesConfig`` already enforce, in one place the API can point at.
VALUE_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ComponentValuesError(ValueError):
    """A values read or write that cannot be done as asked.

    Carries a message meant for the caller: which key is already there, which one is
    missing, or why nothing could be encrypted. Never carries a value.
    """


class ValuesOperation(StrEnum):
    """What a request does to the set of values at one layer."""

    ADD = "add"
    PATCH = "patch"
    DELETE = "delete"
    CLEAR = "clear"


def validate_key(key: str) -> None:
    """Raise unless *key* can be an environment-variable name."""
    if not VALUE_KEY.match(key):
        raise ComponentValuesError(
            f"Ongeldige naam '{key}'. Een naam wordt een omgevingsvariabele en moet beginnen "
            "met A-Z, a-z of _ en verder alleen A-Z, a-z, 0-9 of _ bevatten."
        )


def validate_value(key: str, value: str) -> None:
    """Raise unless *value* survives the wire as one ``KEY=value`` line.

    A newline would split one variable into two (or into a parse error), and a null
    byte cannot travel in an environment variable at all. The offending value is never
    quoted back: it is a secret, and naming the key is enough to find it.
    """
    if "\n" in value or "\r" in value:
        raise ComponentValuesError(
            f"De waarde van '{key}' bevat een regeleinde; dat kan niet in een omgevingsvariabele."
        )
    if "\x00" in value:
        raise ComponentValuesError(
            f"De waarde van '{key}' bevat een null-byte; dat kan niet in een omgevingsvariabele."
        )


def _block_roundtrip(value: str) -> str | None:
    """What ``KEY=value`` gives back when the block is read again, or None if unreadable.

    Measured with the real reader rather than a copy of its rules, so it cannot drift
    away from it.
    """
    try:
        return validate_and_parse_env_vars(f"ZAD_ROUNDTRIP={value}").get("ZAD_ROUNDTRIP")
    except ValueError:
        return None


def validate_value_for_storage(key: str, value: str, storage: ValueStorage) -> None:
    """Raise unless *value* comes back out of *storage* exactly as it went in.

    Two normalisations sit between a write and the next read, and neither is this
    module's to change:

    * ``decrypt_age_content_sync`` strips the plaintext it gets back from ``age`` (it has
      to: the armored form ends in a newline). That hits **both** storage shapes, so a
      value with leading or trailing whitespace never returns as written, aliases
      included.
    * ``ValueStorage.BLOCK`` additionally reads its plaintext with
      :func:`validate_and_parse_env_vars`, which reads ``KEY=value`` the way a shell
      would and removes one layer of surrounding quotes -- so ``'"q"'`` returns as ``q``.

    Storing such a value anyway would break two promises at once. The API would hand back
    something other than what was written, and -- worse -- the stored set would never
    equal the requested one, so the no-op check in
    :meth:`ProjectManager.set_component_values` would find a difference on every call and
    commit to ``zad-projects`` every time, which is exactly the churn the design forbids.
    Refusing up front is the only answer that keeps both promises; quoting or escaping on
    the way in would change what the workload receives.
    """
    if value.strip() != value:
        raise ComponentValuesError(
            f"De waarde van '{key}' begint of eindigt met witruimte, en die overleeft de "
            "versleutelde opslag niet. Lever de waarde aan zonder die spaties, tabs of "
            "regeleindes aan de randen."
        )
    if storage is ValueStorage.BLOCK and _block_roundtrip(value) != value:
        raise ComponentValuesError(
            f"De waarde van '{key}' overleeft de opslagvorm niet. Deze waarden worden als "
            "KEY=value-regels bewaard, en daarbij vervalt een paar omringende aanhalingstekens. "
            "Lever de waarde aan zonder die aanhalingstekens."
        )


#: The layers an owned values property can live on. A component carries it under its own
#: name; a deployment's component carries it under ``reference``, because inside a
#: deployment a component is a reference to one, not a second definition of it.
VALUES_LAYERS = (ConfigLayer.COMPONENT, ConfigLayer.DEPLOYMENT_COMPONENT)


def locate(
    project_data: dict[str, Any],
    layer: ConfigLayer,
    component_name: str,
    deployment_name: str | None = None,
) -> dict[str, Any] | None:
    """The dict in the project file that carries the owned property, or None.

    None means the component (or the deployment, or that component inside it) is not
    there. The caller turns that into a 404 before enqueueing; inside a mutation it is
    the guard against the file having changed since that check.
    """
    components = project_data.get("components") or []
    if layer is ConfigLayer.COMPONENT:
        return next(
            (c for c in components if isinstance(c, dict) and c.get("name") == component_name),
            None,
        )
    deployment = next(
        (d for d in project_data.get("deployments") or [] if isinstance(d, dict) and d.get("name") == deployment_name),
        None,
    )
    if deployment is None:
        return None
    return next(
        (c for c in deployment.get("components") or [] if isinstance(c, dict) and c.get("reference") == component_name),
        None,
    )


def decode(
    raw: Any, storage: ValueStorage, project_data: dict[str, Any], private_key: str | None = None
) -> dict[str, str]:
    """The plaintext values currently stored under a component's owned property.

    *raw* is whatever the project file holds there: absent, an AGE block, a plain
    ``KEY=value`` block, or a mapping (encrypted per value or legacy plaintext). An
    empty/absent property is an empty map, not an error -- "nothing set yet" is the
    normal starting point for an add.

    *private_key* is the project's decoded AGE private key when the caller already holds
    it. Without it the key is decoded from *project_data*, which needs the system key --
    a read path that already resolved the key (the project read endpoints) must pass it
    rather than make this module resolve it a second way.
    """
    if raw is None or raw == "" or raw == {}:
        return {}
    if storage is ValueStorage.BLOCK:
        return _decode_block(raw, project_data, private_key)
    return _decode_per_value(raw, project_data, private_key)


def _decode_block(raw: Any, project_data: dict[str, Any], private_key: str | None = None) -> dict[str, str]:
    if isinstance(raw, dict):
        # Legacy mapping shape, from before the value became a single string.
        return {str(key): str(value) for key, value in raw.items()}
    text = str(raw)
    if is_age_encrypted(text):
        text = _decrypt(text, project_data, "user-env-vars", private_key)
    try:
        return validate_and_parse_env_vars(text)
    except ValueError as error:
        raise ComponentValuesError(f"De opgeslagen omgevingsvariabelen zijn niet te lezen: {error}") from error


def _decode_per_value(raw: Any, project_data: dict[str, Any], private_key: str | None = None) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise ComponentValuesError("De opgeslagen waarden hebben niet de verwachte vorm van een mapping.")
    values: dict[str, str] = {}
    for key, value in raw.items():
        text = str(value)
        values[str(key)] = _decrypt(text, project_data, str(key), private_key) if is_age_encrypted(text) else text
    return values


def _decrypt(ciphertext: str, project_data: dict[str, Any], label: str, private_key: str | None = None) -> str:
    """Decrypt one stored ciphertext, or raise.

    Fail-closed on purpose: returning the armored block as if it were the value would
    hand the ciphertext back to the caller and then re-store it as plaintext on the
    next write.
    """
    key = private_key or get_decoded_project_private_key_sync(project_data)
    plaintext = decrypt_age_content_sync(ciphertext, key)
    if plaintext is None:
        raise ComponentValuesError(f"De opgeslagen waarde van '{label}' is niet te ontsleutelen met de projectsleutel.")
    return plaintext


def encode(values: dict[str, str], storage: ValueStorage, project_data: dict[str, Any]) -> Any:
    """The stored shape for *values*, encrypted with the project's public key.

    Returns ``None`` for an empty map, which is the caller's signal to remove the
    property rather than store an empty one. Raises when there is no public key: this
    is the fail-closed guarantee, and it is why the encryption happens here and not in
    a generator that runs later and may not run at all.
    """
    if not values:
        return None
    public_key = get_project_public_key(project_data)
    if not public_key:
        raise ComponentValuesError(
            "Het project heeft geen AGE-publieke sleutel; waarden worden niet onversleuteld opgeslagen."
        )
    if storage is ValueStorage.BLOCK:
        text = "\n".join(f"{key}={value}" for key, value in values.items())
        return LiteralScalarString(encrypt_age_content_sync(text, public_key))
    return {key: LiteralScalarString(encrypt_age_content_sync(value, public_key)) for key, value in values.items()}


def apply_operation(
    current: dict[str, str],
    operation: ValuesOperation,
    *,
    values: dict[str, str] | None = None,
    keys: list[str] | None = None,
) -> dict[str, str]:
    """The new value map after *operation*, or raise on a name that does not fit it.

    Adding refuses a name that is already there and patching/deleting refuses one that
    is not. Without that a typo in a name silently creates a second variable next to
    the one that was meant, or overwrites a value nobody wanted touched -- and since
    the stored form is encrypted, nobody would see it in the diff either.
    """
    updated = dict(current)
    if operation is ValuesOperation.CLEAR:
        return {}
    if operation is ValuesOperation.DELETE:
        missing = [key for key in (keys or []) if key not in updated]
        if missing:
            raise ComponentValuesError(f"Onbekende naam/namen: {', '.join(sorted(missing))}.")
        for key in keys or []:
            del updated[key]
        return updated

    incoming = values or {}
    if operation is ValuesOperation.ADD:
        existing = [key for key in incoming if key in updated]
        if existing:
            raise ComponentValuesError(
                f"Bestaat al: {', '.join(sorted(existing))}. Gebruik PATCH om een bestaande waarde te wijzigen."
            )
    else:
        missing = [key for key in incoming if key not in updated]
        if missing:
            raise ComponentValuesError(
                f"Onbekende naam/namen: {', '.join(sorted(missing))}. Gebruik POST om een nieuwe waarde toe te voegen."
            )
    updated.update(incoming)
    return updated
