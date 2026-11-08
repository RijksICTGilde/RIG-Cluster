"""Flow ID context variable for correlating log lines within a single execution flow.

Provides a short, human-readable ID that is automatically injected into every log
line via FlowIdFilter. Works with both sync and async code via contextvars.
"""

import contextvars
import uuid

flow_id: contextvars.ContextVar[str] = contextvars.ContextVar("flow_id", default="-")


def set_flow_id(prefix: str) -> str:
    """The prefix names the kind of flow, such as "req" or "task"."""
    new_id = f"{prefix}-{uuid.uuid4().hex[:8]}"
    flow_id.set(new_id)
    return new_id


def get_flow_id() -> str:
    """Return the current flow ID, or '-' if none is set."""
    return flow_id.get()
