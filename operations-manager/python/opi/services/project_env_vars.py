"""Reading a component's own environment variables back out of a project file (RC-61).

``user-env-vars`` is stored as an AGE-encrypted block, so there is no way to know even
the *names* of the variables without decrypting. Both readers that need them -- the
project detail page and the read-only API -- therefore run the same decrypt-and-parse
path, and it lives here so there is exactly one of it: two copies would drift, and a
drifting decrypt path is how a value ends up somewhere a name was intended.

The parsing is deliberately the same two-step the stored shapes require: the value may
be YAML (a mapping) or a ``KEY=value`` block, and which one is not recorded anywhere.
See ``opi/services/catalog/user_env_vars/config_model.py`` for the three legal shapes.
"""

from __future__ import annotations

import logging

from opi.utils.age import decrypt_age_content, is_age_encrypted
from opi.utils.env_vars import validate_and_parse_env_vars
from opi.utils.yaml_util import load_yaml_from_string

logger = logging.getLogger(__name__)


async def read_user_env_vars(
    raw: str | dict[str, str] | None,
    project_private_key: str,
    *,
    where: str,
) -> dict[str, str] | None:
    """Decrypt and parse a stored ``user-env-vars`` value into a name -> value mapping.

    Args:
        raw: What the project file holds: an AGE-encrypted block, a plain KEY=value or
            YAML text block, a mapping, or nothing at all.
        project_private_key: The project's AGE private key.
        where: Which component this is, for the log line only.

    Returns:
        The parsed variables, or None when the stored value could not be read. Nothing
        stored -- the key is absent, or present but empty -- is an empty mapping: we
        looked and there are none. None means "unknown", never "empty": a caller must
        not present it as "this component has no variables".
    """
    if not raw:
        # Nothing stored is an answer, not a failure: we looked, there are none.
        return {}
    if isinstance(raw, dict):
        return dict(raw)

    try:
        # A plain block is legal storage, and handing it to age would fail; only an
        # actual encrypted block goes through decryption.
        text = await decrypt_age_content(raw, project_private_key) if is_age_encrypted(raw) else raw

        parsed = load_yaml_from_string(text)
        if isinstance(parsed, str) or parsed is None:
            # YAML accepted it as a scalar (or refused it): it is the KEY=value shape.
            parsed = validate_and_parse_env_vars(text)
    except Exception as exc:
        # Never the value, never the names: that a read failed, and for which component,
        # is all a log may say about someone's environment variables.
        logger.warning(f"Failed to read user-env-vars for {where}: {exc}")
        return None

    if not isinstance(parsed, dict):
        logger.warning(f"Unexpected user-env-vars shape for {where}: {type(parsed).__name__}")
        return None
    return {str(key): value for key, value in parsed.items()}
