"""What a task can say about a failure that is not the platform's fault.

A handler can fail in two ways: it returns a failure dict (permanent, no new attempt) or it
raises (the worker tries again). That distinction is exactly right for an outage that blows
over, and exactly wrong for a request that cannot work: "this component does not exist" does
not become true on a second attempt.

``TaskInputError`` is the raising form of "the caller asked for something that cannot work".
The worker turns it into a permanent failure WITH the ``error_type`` the raiser handed over,
so a client can attribute it instead of seeing it as unexplainable. Translating that type
into a category does not happen here but where a task record becomes an answer
(``opi/api/task_models.py``), so that there is one translation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from opi.connectors.git import GitCommandTimeoutError, GitRemoteUnavailableError
from opi.connectors.mail import MailRelayUnavailableError
from opi.core.errors import ErrorCategory

if TYPE_CHECKING:
    from opi.manager.project_manager import ProjectManager

#: What an error is when the caller did nothing wrong and we know no more than that.
INTERNAL_ERROR_TYPE = "internal_error"

#: What a 404 out of a manager means for the caller of a task.
NOT_FOUND_ERROR_TYPE = "not_found"

#: What every other 4xx means: the request itself is no good.
INVALID_REQUEST_ERROR_TYPE = "invalid_request"

#: The ``error_type`` values that say: the caller asked for something that cannot work. A task
#: failing on one of these is not an outage, so it is logged at WARNING and not at ERROR. This
#: is the only list; the types themselves come from the managers, none is invented here.
CLIENT_ERROR_TYPES = frozenset(
    {
        "validation_error",
        "invalid_component_references",
        # The upsert refuses on these four as well: an ambiguous or missing repository, a
        # domain the project may not have, and a concurrent edit that could not be merged.
        # The synchronous route has always answered 4xx for them, so the asynchronous one
        # must not log them as an outage.
        "ambiguous_repository",
        "no_repositories",
        "domain_validation",
        "conflict",
        NOT_FOUND_ERROR_TYPE,
        INVALID_REQUEST_ERROR_TYPE,
    }
)


class TaskInputError(Exception):
    """The caller asked for something that cannot work; trying again changes nothing.

    ``error_type`` is the specific reason, in the same words the managers already use
    (``deployment_not_found``, ``component_not_found``, ...), so that no second vocabulary
    comes into being.
    """

    def __init__(self, message: str, error_type: str = INVALID_REQUEST_ERROR_TYPE) -> None:
        super().__init__(message)
        self.error_type = error_type


#: Exceptions that say: the PLATFORM stumbled, not the project. An unreachable mail relay, a
#: git server that does not answer and a git command that times out all three belong here:
#: there is nothing in the project to repair, and the message says so already.
PLATFORM_FAILURES: tuple[type[BaseException], ...] = (
    MailRelayUnavailableError,
    GitRemoteUnavailableError,
    GitCommandTimeoutError,
)


def platform_failure_category(exception: BaseException | None) -> dict[str, str]:
    """``error_category: InternalError`` when this error came from the platform.

    ``processing_failed`` lands at a client as "not attributable", and that is honest as long
    as a component that does not become healthy can also be the user's image. An outage out of
    :data:`PLATFORM_FAILURES` is not.

    ``_with_error_category`` leaves a category the handler sets itself alone, so this is all
    the handler has to do.
    """
    if isinstance(exception, PLATFORM_FAILURES):
        return {"error_category": ErrorCategory.InternalError.value}
    return {}


def platform_failure_fields(project_manager: ProjectManager) -> dict[str, str]:
    """The same category, for a handler that reads the exception out of the manager."""
    return platform_failure_category(project_manager.get_processing_exception())
