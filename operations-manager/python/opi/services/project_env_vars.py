"""Reading a component's own environment variables back out of a project file (RC-61).

``user-env-vars`` is stored as an AGE-encrypted block, so there is no way to know even
the *names* of the variables without decrypting. The reading itself is
``opi.services.component_values.decode``, which the write path uses as well; this module
is the adapter the READ endpoints need around it, and the only difference is the failure
shape: a page or a listing that shows many components must not break over one block it
cannot read, so an unreadable block is ``None`` here instead of an exception.
"""

from __future__ import annotations

import asyncio
import logging

from opi.services.component_values import ComponentValuesError, decode

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
    try:
        # In a thread: decrypting is in-process CPU work (``pyrage``), and this runs in
        # the event loop that has to answer the rest of the page.
        return await asyncio.to_thread(decode, raw, {}, project_private_key)
    except (ComponentValuesError, ValueError, TypeError, OSError) as error:
        # Never the value, never the names: that a read failed, and for which component,
        # is all a log may say about someone's environment variables.
        logger.warning(f"Failed to read user-env-vars for {where}: {error}")
        return None
