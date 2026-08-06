"""Extra API actions a service declares for itself (RC-38).

The generic config endpoints cover "configure this service at this layer", and they are
generated from what the service's *editables* declare. That works for most of the catalog
and breaks exactly where a service can do something the wizard has no field for. The
measured case: an API client could point a component at attachment ``my-cert`` but had no
way to put ``my-cert`` into the project, because uploading a file is not a config block --
it is the DEFINE side, and it arrives as multipart.

So a service may declare extra actions. A declaration says, once:

* which layer it acts on and what it does there (``ConfigRole``),
* which fields it takes, what each field *means* (the text lands in the OpenAPI
  document), and which rule validates it,
* which verbs it supports and what each verb does when the id already exists,
* which field combinations are valid, and where that rule is actually enforced,
* an example of a call.

Route, request signature and documentation are derived from that (see
``_register_service_action_routes`` in ``opi/api/v2/router.py``), instead of the same
knowledge being written out three times.

**A field points at an editable; it never restates a rule.** This is the same move
``opi/api/validation.py`` made for the component endpoints: the API references the shared
``Editable`` that the wizard renders, so "what may this value look like" has exactly one
definition. A field that genuinely has no editable (a file's bytes) says so in
``no_editable_reason`` -- a written exception, not a second validator.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from opi.forms.editables.editable import Editable
    from opi.services.catalog.base import ConfigLayer, ConfigRole


class ActionVerb(Enum):
    """The verbs an action can support, and what each one promises.

    The distinction is the contract, decided on 6 August 2026 and deliberately not
    softened: ``CREATE`` refuses to touch something that already exists, ``UPDATE``
    expects it to exist, and ``UPSERT`` replaces whatever is there on id, without asking.

    Replacing without warning is only ever what ``UPSERT`` does, and it has to be called
    as such. A ``POST`` that silently overwrites lies about what it did, and the caller
    finds out when the old file is gone. Whether replacing was the intention is the
    caller's business -- which is exactly why the caller has to say it in the request.
    """

    CREATE = "create"
    UPDATE = "update"
    UPSERT = "upsert"

    @property
    def method(self) -> str:
        """The HTTP method this verb is served on."""
        return "POST" if self is ActionVerb.CREATE else "PUT"

    @property
    def targets_existing(self) -> bool:
        """Whether the request addresses one existing item (id in the path)."""
        return self is not ActionVerb.CREATE

    @property
    def on_existing(self) -> str:
        """What happens when the id is already taken: ``reject`` or ``replace``."""
        return "reject" if self is ActionVerb.CREATE else "replace"

    @property
    def on_absent(self) -> str:
        """What happens when the id does not exist yet: ``create`` or ``reject``."""
        return "reject" if self is ActionVerb.UPDATE else "create"


class ActionFieldKind(Enum):
    """How a field arrives in the request."""

    #: A multipart text field.
    TEXT = "text"
    #: An uploaded file; the handler receives its bytes.
    FILE = "file"


@dataclass(frozen=True)
class ActionField:
    """One field of an action: what it means, and which rule already validates it."""

    name: str
    #: What this field is, in one sentence. Ends up in the OpenAPI document, so it is
    #: written for someone holding a generated client, not for someone reading this file.
    description: str
    #: The shared Editable that defines what a valid value looks like. The API runs this
    #: same rule the wizard runs, so there is one definition of the rule per field.
    editable: Editable | None = None
    #: Why this field has no editable. Required when ``editable`` is None, so a missing
    #: rule is a decision on the record rather than an omission nobody notices.
    no_editable_reason: str = ""
    kind: ActionFieldKind = ActionFieldKind.TEXT
    #: The verbs that insist on this field. A verb not listed accepts it as optional.
    required_for: tuple[ActionVerb, ...] = ()
    #: A value that shows what this field looks like, for the OpenAPI example.
    example: str = ""

    def __post_init__(self) -> None:
        if self.editable is None and not self.no_editable_reason:
            raise ValueError(
                f"Action field '{self.name}' has no editable and no reason. Point it at the shared "
                f"Editable that already validates this value, or write down why there is none."
            )
        if not self.description:
            raise ValueError(f"Action field '{self.name}' has no description")

    def is_required_for(self, verb: ActionVerb) -> bool:
        return verb in self.required_for


@dataclass(frozen=True)
class FieldCombination:
    """A rule about which fields go together, and where it is enforced.

    Documentation with a pointer, never an implementation: the rule itself lives in the
    model or the editable that already has it (an attachment delivered as a file needs a
    path -- ``AttachmentUse`` says so, and refusing to say it twice is the point).
    ``enforced_by`` is a dotted path, and a test resolves it, so the pointer cannot rot
    into a claim about a check that no longer exists.
    """

    #: The condition, in the caller's terms (e.g. ``provide-as=file``).
    when: str
    #: The fields that condition makes mandatory.
    requires: tuple[str, ...]
    #: Dotted path to the code that enforces it.
    enforced_by: str


@dataclass(frozen=True)
class UploadedFile:
    """One uploaded file as a handler sees it: its name and its bytes, already read."""

    filename: str
    content: bytes


@dataclass
class ActionContext:
    """What a handler is given: the addressed thing and the validated values."""

    project_name: str
    verb: ActionVerb
    #: Field name -> value. A FILE field carries ``bytes``; the rest carry strings.
    values: dict[str, Any]
    #: The id in the path for UPDATE/UPSERT; None for CREATE (the caller supplies it as
    #: a field, because there is nothing to address yet).
    item_id: str | None = None
    #: The component being acted on, for a COMPONENT-layer action.
    component_name: str | None = None


@dataclass
class ActionResult:
    """What a handler answers: an HTTP status and a body.

    A handler returns its refusals (409 on an existing id, 404 on an absent one) rather
    than raising, so a service never has to import the web framework to say "no".
    """

    status_code: int
    body: dict[str, Any]


@dataclass(frozen=True)
class ServiceAction:
    """One extra API action, declared by the service that owns it."""

    #: The last path segment of the route, e.g. ``attachments``.
    action_id: str
    #: The layer this action acts on. PROJECT -> ``/services/{svc}/{action_id}``,
    #: COMPONENT -> ``/services/{svc}/component/{component}/{action_id}``.
    layer: ConfigLayer
    #: What this action does at that layer, in the vocabulary of ``ConfigRole``.
    roles: tuple[ConfigRole, ...]
    #: One line for the OpenAPI summary.
    summary: str
    #: The full explanation for the OpenAPI description, including what each verb does.
    description: str
    fields: tuple[ActionField, ...]
    verbs: tuple[ActionVerb, ...]
    handler: Callable[[ActionContext], Awaitable[ActionResult]]
    #: How the item is addressed: the path parameter name for UPDATE/UPSERT.
    id_param: str = "item_id"
    combinations: tuple[FieldCombination, ...] = ()
    #: A worked example of calling this action (a curl line), shown in the OpenAPI
    #: description. Concrete on purpose: the fields alone never show what a real call
    #: looks like, and this is the first thing anyone integrating reads.
    example: str = ""

    def __post_init__(self) -> None:
        if not self.verbs:
            raise ValueError(f"Action '{self.action_id}' declares no verbs")
        if not self.example:
            raise ValueError(f"Action '{self.action_id}' has no usage example")
        names = [f.name for f in self.fields]
        if len(names) != len(set(names)):
            raise ValueError(f"Action '{self.action_id}' declares a field twice: {names}")
        for combination in self.combinations:
            unknown = set(combination.requires) - set(names)
            if unknown:
                raise ValueError(f"Action '{self.action_id}' requires unknown fields {unknown} in a combination")

    def editables_for(self, verb: ActionVerb) -> dict[str, Editable]:
        """The validation profile for one verb: field name -> shared Editable.

        Built the way ``opi/api/validation.py`` builds its profiles -- the shared editable
        with ``required`` set to what *this endpoint* insists on. Whether a field may be
        left out is the endpoint's business; what a valid value looks like is the field's,
        and that half is never copied here.
        """
        profile: dict[str, Editable] = {}
        for action_field in self.fields:
            if action_field.editable is None:
                continue
            profile[action_field.name] = replace(action_field.editable, required=action_field.is_required_for(verb))
        return profile
