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

from dataclasses import dataclass, field, replace
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

    ``DELETE`` joined them on 7 August 2026: the catalog could be written and rewritten but
    never emptied, so entries piled up with no way back. It addresses one item by path and
    takes nothing else (see ``takes_fields``); what it needs beyond that identity is a flag
    the caller sets, not a body.
    """

    CREATE = "create"
    UPDATE = "update"
    UPSERT = "upsert"
    DELETE = "delete"

    @property
    def method(self) -> str:
        """The HTTP method this verb is served on."""
        if self is ActionVerb.CREATE:
            return "POST"
        if self is ActionVerb.DELETE:
            return "DELETE"
        return "PUT"

    @property
    def targets_existing(self) -> bool:
        """Whether the request addresses one existing item (id in the path)."""
        return self is not ActionVerb.CREATE

    @property
    def takes_fields(self) -> bool:
        """Whether this verb carries the action's fields in a request body.

        A delete says which item and nothing more: the path is the whole request, and the
        fields that describe an item's *content* have no meaning when the item is going
        away. An optional file on a DELETE would be a body a caller could fill in and have
        silently ignored. Anything a delete still needs to be told is a flag.
        """
        return self is not ActionVerb.DELETE

    @property
    def on_existing(self) -> str:
        """What happens when the id is already taken: ``reject``, ``replace`` or ``remove``."""
        if self is ActionVerb.CREATE:
            return "reject"
        return "remove" if self is ActionVerb.DELETE else "replace"

    @property
    def on_absent(self) -> str:
        """What happens when the id does not exist yet: ``create`` or ``reject``."""
        return "reject" if self in (ActionVerb.UPDATE, ActionVerb.DELETE) else "create"


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
    #: Whether a route that addresses one existing item carries this field in its path
    #: instead of its body. True for the id, and for anything that says the same thing
    #: the id says (a reference to the very item the path already names).
    addressed_by_path: bool = False
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
class FieldDisjunction:
    """A rule that exactly one of these fields is given, and where it is enforced.

    ``FieldCombination`` is an implication -- *if* this, *then* that -- and an either/or
    can only be written as two of those, with nothing holding the two together: neither
    side says that giving both is wrong, or that giving neither is. "Send A or B" is a
    rule of its own, so it is declared as one.

    Same discipline as the implication, for the same reason: this is documentation with a
    pointer. ``enforced_by`` names the code that actually refuses the request, a test
    resolves it, and the declaration itself validates nothing -- a disjunction that
    started checking would put the rule in two places, which is exactly what pointing at
    the enforcer avoids.

    It reaches the OpenAPI document as ``oneOf`` over the alternatives, so a client reads
    the rule off the spec instead of discovering it at the 422.
    """

    #: The fields the caller chooses between; exactly one of them is given.
    one_of: tuple[str, ...]
    #: What the choice is, in one sentence, in the caller's terms.
    describes: str
    #: Dotted path to the code that enforces it.
    enforced_by: str

    def __post_init__(self) -> None:
        if len(self.one_of) < 2:
            raise ValueError(f"A disjunction needs at least two alternatives, got {self.one_of}")


@dataclass(frozen=True)
class ActionFlag:
    """A boolean query parameter one of an action's routes takes.

    A field describes the *thing* a request is about; a flag describes what the caller
    wants done and what they already know. ``upsert`` was the first of these and lives in
    the verb table, because replacing is a promise about an id. ``confirm_in_use`` is the
    second and belongs to the service: only the attachments service knows what "in use"
    means for an attachment.

    Off by default, always. A flag exists because the default answer is a refusal and the
    caller has to overrule it in the request -- a flag that defaults to on is just
    behaviour with an extra name.
    """

    name: str
    #: What setting it means, in one sentence. Lands in the OpenAPI parameter description,
    #: so it is written for someone deciding whether to set it.
    description: str
    #: The verbs whose routes take it. A flag on a verb the action does not declare is a
    #: parameter nobody can reach, so the action rejects it.
    verbs: tuple[ActionVerb, ...]

    def __post_init__(self) -> None:
        if not self.description:
            raise ValueError(f"Action flag '{self.name}' has no description")
        if not self.verbs:
            raise ValueError(f"Action flag '{self.name}' applies to no verb")


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
    #: The declared flags this route took, by name. Absent flags are not in the map, so a
    #: handler reads them with a default rather than trusting every route to fill them in.
    flags: dict[str, bool] = field(default_factory=dict)


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
    #: The either/or rules over this action's fields (see ``FieldDisjunction``).
    disjunctions: tuple[FieldDisjunction, ...] = ()
    #: The boolean query parameters this action's routes take (see ``ActionFlag``).
    flags: tuple[ActionFlag, ...] = ()
    #: The task type that rolls this action's change out to the cluster, when it has one.
    #:
    #: An action without it only writes the project file, and the change reaches the
    #: cluster whenever something else happens to process the project -- which is a
    #: coincidence, not a contract. That was the attachments bug: a replaced certificate
    #: was committed and nothing ever generated a manifest from it.
    #:
    #: Declaring it changes the route's contract. It gains the ``rollout`` query parameter
    #: every other mutating endpoint has, and answers 202 with a task id instead of a
    #: synchronous 200/201: the write and its validation still happen in the request (the
    #: file arrives as multipart and does not belong in a task payload), the processing
    #: that follows does not. Refusals stay synchronous, so a 409 or a 422 is still the
    #: immediate answer it was.
    rollout_task_type: str | None = None
    #: A worked example of calling this action (a curl line), shown in the OpenAPI
    #: description. Concrete on purpose: the fields alone never show what a real call
    #: looks like, and this is the first thing anyone integrating reads.
    example: str = ""
    #: Per-verb examples, for verbs the general example does not show. A DELETE route
    #: documented with a ``curl -X POST -F file=@...`` line is worse than no example: it
    #: is an example of a different request.
    verb_examples: tuple[tuple[ActionVerb, str], ...] = ()

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
        for disjunction in self.disjunctions:
            unknown = set(disjunction.one_of) - set(names)
            if unknown:
                raise ValueError(f"Action '{self.action_id}' offers unknown fields {unknown} in a disjunction")
        flag_names = [flag.name for flag in self.flags]
        if len(flag_names) != len(set(flag_names)):
            raise ValueError(f"Action '{self.action_id}' declares a flag twice: {flag_names}")
        for flag in self.flags:
            unreachable = set(flag.verbs) - set(self.verbs)
            if unreachable:
                raise ValueError(
                    f"Action '{self.action_id}' offers flag '{flag.name}' on verbs it does not declare: "
                    f"{sorted(v.value for v in unreachable)}"
                )

    def editables_for(self, verb: ActionVerb) -> dict[str, Editable]:
        """The validation profile for one verb: field name -> shared Editable.

        Built the way ``opi/api/validation.py`` builds its profiles -- the shared editable
        with ``required`` set to what *this endpoint* insists on. Whether a field may be
        left out is the endpoint's business; what a valid value looks like is the field's,
        and that half is never copied here.

        A verb that takes no fields has an empty profile rather than a profile of optional
        fields: there is nothing to send, so there is nothing to validate.
        """
        if not verb.takes_fields:
            return {}
        profile: dict[str, Editable] = {}
        for action_field in self.fields:
            if action_field.editable is None:
                continue
            profile[action_field.name] = replace(action_field.editable, required=action_field.is_required_for(verb))
        return profile

    def flags_for(self, verbs: tuple[ActionVerb, ...]) -> tuple[ActionFlag, ...]:
        """The flags the route serving ``verbs`` takes."""
        return tuple(flag for flag in self.flags if set(flag.verbs) & set(verbs))

    def example_for(self, verbs: tuple[ActionVerb, ...]) -> str:
        """The example to show on the route serving ``verbs``, most specific first."""
        for verb, example in self.verb_examples:
            if verb in verbs:
                return example
        return self.example
