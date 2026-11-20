"""Generic approval capability for the service catalog.

A service declares -- as data -- that some value it manages needs approval before it
takes effect ("dit heeft approval nodig"), and supplies the rule that reads the stored
approval state back ("is dit veld approved?"). Generic code (the approver interface and
the enforcers) consumes these ``ApprovalSpec``s uniformly.

Publish-on-web's domain / subdomain approval is the exception to that shape: its state
lives in the project's ``domains:`` block and its rules are the predicates in
``services/catalog/publish_on_web/subdomain.py``, which the spec wraps rather than owns.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from opi.services.project import Project
from opi.services.services import service_entry_config, service_entry_name

if TYPE_CHECKING:
    from collections.abc import Callable

    from opi.services.services_enums import ServiceType


class ApprovalStatus(StrEnum):
    """Where a requested value sits in the approval lifecycle.

    The three non-NONE values match the strings persisted in the project file
    (``requested`` / ``approved`` / ``denied``), so a stored status maps straight onto
    the enum. NONE means "no request on record" -- nothing to approve yet.
    """

    NONE = "none"
    REQUESTED = "requested"
    APPROVED = "approved"
    DENIED = "denied"


class ApproverScope(StrEnum):
    """Who is entitled to approve a value.

    Domains are cross-project, so a PLATFORM_ADMIN approves them; something granted within
    one project is approved by a PROJECT_ADMIN or PROJECT_MEMBER. The scope selects which
    approver a generic approval interface routes a pending item to.
    """

    PLATFORM_ADMIN = "platform-admin"
    PROJECT_ADMIN = "project-admin"
    PROJECT_MEMBER = "project-member"


#: Wire shape of an approval **item** -- one approvable thing found in a project, as
#: the approver interface (listing + modal flow) consumes it. Transient: it is seeded
#: into the wizard, never persisted (stripped before the project is saved). A plain dict,
#: not a dataclass: the approval editables and ``approval_items.html.j2`` read these keys.
#: Keys:
#:   ``service``        -- the ServiceType value of the owning service (routing)
#:   ``type``           -- the owning ``ApprovalSpec.key`` (routing + display)
#:   ``domain``/``name``-- display identity of the item
#:   ``subject``        -- WHAT is being asked for, written by the service that knows
#:                         (``example.nl``, ``foo.example.nl``, "Gebruik van de dienst").
#:                         Display only: it does not travel back through the modal form.
#:   ``current_status`` -- the stored status string
#:   ``status``         -- the approver's verdict ("skip" until decided)
#:   ``history``        -- prior verdict entries
#:   ``message``        -- optional note (added by the approver)
ApprovalItem = dict[str, Any]


#: Wire shape of an approval **notice** -- what the OWNER of a deployment must be told
#: about an approval that is not granted. The counterpart of ApprovalItem: that one
#: faces the approver ("decide this"), this one faces the applicant ("this is where
#: your request stands, and this is what it means for your deployment"). The consequence
#: is service knowledge (publish-on-web falls back to the cluster address), so the spec
#: writes the sentence and generic code only renders it. Keys:
#:   ``service``  -- the ServiceType value of the owning service
#:   ``type``     -- the owning ``ApprovalSpec.key``
#:   ``label``    -- the spec's human label ("Domein", "Subdomein")
#:   ``subject``  -- what was requested, for display (e.g. "test2.example.nl")
#:   ``status``   -- the stored status ("requested" / "denied" / "none")
#:   ``text``     -- what this means for the deployment, in the owner's words
#:   ``by`` / ``date`` / ``message`` -- the last verdict's approver, date and note
ApprovalNotice = dict[str, Any]


@dataclass(frozen=True)
class ApprovalSpec:
    """A service's declaration that a value it manages requires approval.

    This is the DEFINITION ("dit heeft approval nodig"): a service returns one or more
    of these from ``config_approvals(layer)``. It is pure data plus rule callbacks --
    it carries no forms / manager / DB imports, so the catalog stays load-light.

    Attributes:
        key: Stable identifier for this approvable within the service (e.g. ``"domain"``,
            ``"subdomain"``). Used to look the spec up and to key any UI / state.
        label: Human label for the approver-facing interface.
        approver: Who may approve (see :class:`ApproverScope`).
        status_of: The CHECK ("is dit veld approved?"). Given the project data and the
            value being requested, returns its :class:`ApprovalStatus` by reading the
            stored approval state. The service owns this rule; generic code just calls
            it. The ``value`` is opaque to the generic layer -- its shape is the
            service's business (a domain string, a ``(domain, subdomain)`` pair, ...).
        list_items: The LIST ("wat staat er open voor deze aanvraag?"). Enumerates the
            approvable items this spec currently has in a project, as
            :data:`ApprovalItem` dicts. The generic approver interface concatenates these
            across the catalog. ``None`` for a check-only spec that does not surface in the
            approver UI.
        record: The RECORD ("leg het oordeel vast"). Applies one approver verdict --
            writes the new status + appends ``history_entry`` to the stored state for
            the given item. ``None`` if the spec is not approver-writable.
        notices_for: The NOTICE ("wat merkt de aanvrager hiervan?"). Given a deployment,
            returns what its owner must be told about this approval when it is not
            granted -- including the consequence, which only the service knows. Returns
            an empty list when there is nothing to report. ``None`` for a spec whose
            state has no visible effect on a deployment.
        approved_values: The MEMORY ("wat staat er goedgekeurd?"). Every last-approved
            value this spec has stored, keyed by the identity of the thing it belongs to
            (empty string for a project-wide value, otherwise the deployment or item
            name). ``validate_approval_decisions`` compares it across a save: such a
            value may only appear or change on the approval route. ``None`` for a spec
            that keeps no approved value.
    """

    key: str
    label: str
    approver: ApproverScope
    status_of: Callable[[dict[str, Any], Any], ApprovalStatus]
    list_items: Callable[[dict[str, Any]], list[ApprovalItem]] | None = None
    record: Callable[[dict[str, Any], ApprovalItem, dict[str, Any]], None] | None = None
    notices_for: Callable[[dict[str, Any], dict[str, Any]], list[ApprovalNotice]] | None = None
    requires_approval_for: Callable[[Any], bool] | None = None
    approved_values: Callable[[dict[str, Any]], dict[str, Any]] | None = None

    def status(self, project_data: dict[str, Any], value: Any) -> ApprovalStatus:  # noqa: ANN401  # a project-file value
        """The approval status of ``value`` given the current project state."""
        return self.status_of(project_data, value)

    def is_approved(self, project_data: dict[str, Any], value: Any) -> bool:  # noqa: ANN401  # a project-file value
        """Whether ``value`` is approved -- the common yes/no check for gating."""
        return self.status_of(project_data, value) is ApprovalStatus.APPROVED

    def requires_approval(self, config: Any) -> bool:  # noqa: ANN401  # a project-file value
        """Whether a service enrolling with ``config`` needs a request for this.

        Decides whether the service may enrol implicitly (``implicit_project_entry``). The
        default is yes: an approval about the service itself applies whatever the config. An
        approval about a VALUE fills this with a predicate of its own, so only the state that
        asks for a decision holds up the implicit enrolment.
        """
        return True if self.requires_approval_for is None else self.requires_approval_for(config)


class _ApprovalState:
    """The approval block of a service: where it lives, and how to read and write it.

    The block hangs under the service config at a fixed key and carries ``status`` and
    ``history``. The forms below share this access so read, create, and update live in one place.
    """

    def __init__(
        self,
        service: str,
        config_key: str,
        holder: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        self.service = service
        self.config_key = config_key
        #: Where the services list carrying the BLOCK lives; an approval at the deployment layer
        #: passes the deployment, so read and write hit its own ``services``. The service
        #: SELECTION stays a query to the project: a deployment does not pick the service.
        self._holder = holder or (lambda project_data: project_data)

    def selected(self, project_data: dict[str, Any]) -> bool:
        """Whether the service appears in the project's services list at all."""
        return self.service in [service_entry_name(entry) for entry in project_data.get("services", []) or []]

    def entry(self, project_data: dict[str, Any]) -> dict[str, Any] | None:
        """The service entry of this service, if it stands as a dict with the holder."""
        for entry in self._holder(project_data).get("services", []) or []:
            if service_entry_name(entry) == self.service and isinstance(entry, dict):
                return entry
        return None

    def service_config(self, project_data: dict[str, Any]) -> dict[str, Any]:
        """The project config of this service, empty if it is not there."""
        config = service_entry_config(self.entry(project_data))
        return config if isinstance(config, dict) else {}

    def _block_holder(self, project_data: dict[str, Any], *, create: bool) -> dict[str, Any] | None:
        """The dict the block ITSELF lives in, created when ``create``.

        Overridable: a block that sits one level deeper (one per item of a list) only has to
        bend this, so that reading, writing and clearing stay shared.
        """
        if not create:
            return self.service_config(project_data)
        entry = self._entry_for_write(project_data)
        if entry is None:
            return None
        config = self._config_holder(entry, self.service).setdefault("config", {})
        return config if isinstance(config, dict) else None

    def read(self, project_data: dict[str, Any]) -> dict[str, Any] | None:
        """The stored block, or None if nothing has been requested."""
        block = (self._block_holder(project_data, create=False) or {}).get(self.config_key)
        return block if isinstance(block, dict) else None

    def status(self, project_data: dict[str, Any]) -> ApprovalStatus:
        """The stored status, NONE while there is no block or no valid status."""
        block = self.read(project_data)
        if block is None:
            return ApprovalStatus.NONE
        try:
            return ApprovalStatus(block.get("status", ""))
        except ValueError:
            return ApprovalStatus.NONE

    def last_verdict(self, project_data: dict[str, Any]) -> dict[str, Any]:
        """The last verdict from history, empty if there is none."""
        history = (self.read(project_data) or {}).get("history") or []
        last = history[-1] if isinstance(history, list) and history else None
        return last if isinstance(last, dict) else {}

    def _promote_bare_entry(self, project_data: dict[str, Any]) -> dict[str, Any] | None:
        """Make a bare-string entry into record form so there is a config to write into.

        A service with only optional config stays as a bare string in the list and has no
        place for the block; the writer would return silently and the request vanish.
        Promoting to record form is what ``set_service_config`` does: selection is a set by
        name, entry form is incidental.
        """
        services = self._holder(project_data).get("services")
        if not isinstance(services, list):
            return None
        for index, entry in enumerate(services):
            if entry == self.service:
                record: dict[str, Any] = {"name": self.service}
                services[index] = record
                return record
        return None

    def _entry_for_write(self, project_data: dict[str, Any]) -> dict[str, Any] | None:
        """The service entry the block goes into, created when a SUB-holder has none.

        A deployment never picks the service itself, so it carries no entry until the
        platform records a verdict there. At project level an absent entry means the service
        is not selected, and writing one would enrol the project in it.
        """
        entry = self.entry(project_data) or self._promote_bare_entry(project_data)
        if entry is not None:
            return entry
        holder = self._holder(project_data)
        if holder is project_data or not holder:
            return None
        services = holder.setdefault("services", [])
        if not isinstance(services, list):
            return None
        record: dict[str, Any] = {"name": self.service}
        services.append(record)
        return record

    @staticmethod
    def _config_holder(entry: dict[str, Any], service: str) -> dict[str, Any]:
        """The dict the CONFIG of this service lives in.

        For record form (``{"name": "send-email", "config": ...}``) it is the entry itself;
        for legacy single-key form (``{"send-email": {"config": ...}}``) it is the inner dict.
        Without that distinction the writer puts the approval block BESIDE the service
        instead of inside it: the reader does not find it, and the entry becomes a two-key
        dict that ``service_entry_name`` warns of.
        """
        inner = entry.get(service)
        if service in entry and isinstance(inner, dict) and "name" not in entry and "reference" not in entry:
            return inner
        return entry

    def write(
        self,
        project_data: dict[str, Any],
        fields: dict[str, Any],
        history_entry: dict[str, Any] | None = None,
    ) -> None:
        """Fill the block, and create it if it does not exist."""
        config = self._block_holder(project_data, create=True)
        if config is None:
            return
        block = config.setdefault(self.config_key, {})
        if not isinstance(block, dict):
            return
        block.update(fields)
        history = block.setdefault("history", [])
        if history_entry is not None and isinstance(history, list):
            history.append(history_entry)

    def clear(self, project_data: dict[str, Any]) -> None:
        """Remove the block so a next request starts from zero.

        In a sub-holder the entry goes with it once it holds nothing else: the deployment
        never picked the service, so an entry without config is a leftover.
        """
        (self._block_holder(project_data, create=False) or {}).pop(self.config_key, None)
        holder = self._holder(project_data)
        entry = self.entry(project_data)
        services = holder.get("services")
        if holder is project_data or entry is None or not isinstance(services, list):
            return
        if entry.get("config") == {}:
            entry.pop("config", None)
        if set(entry) <= {"name", "reference"}:
            services.remove(entry)
        if not services:
            holder.pop("services", None)


def record_migration_approval(
    state: _ApprovalState,
    project_data: dict[str, Any],
    value: Any,  # noqa: ANN401  # a project-file value
    message: str,
) -> None:
    """Write an APPROVED block for ``value`` as a schema migration, with its own history line.

    A state that has been legal until now and needs a decision from this version on is carried
    over rather than withdrawn: the alternative is that every project that already had it sees
    its value silently drop on the next render. The only writer besides the approval route, and
    only from a migration step, which runs once per file.
    """
    state.write(
        project_data,
        {"status": ApprovalStatus.APPROVED.value, "value": value, LAST_APPROVED_KEY: value},
        {
            "date": datetime.now(UTC).isoformat(),
            "status": ApprovalStatus.APPROVED.value,
            "by": "schema-migration",
            "message": message,
        },
    )


def _notice_text(activity: str, consequence: str, status: ApprovalStatus) -> str:
    """The three sentences for the requester, in one place so they stay together."""
    if status is ApprovalStatus.DENIED:
        return f"{activity} is afgewezen. {consequence}".rstrip()
    if status is ApprovalStatus.REQUESTED:
        return f"{activity} is aangevraagd en wacht op goedkeuring. {consequence}".rstrip()
    return f"{activity} is nog niet aangevraagd. {consequence}".rstrip()


def _notice(
    state: _ApprovalState,
    project_data: dict[str, Any],
    *,
    subject: str,
    status: ApprovalStatus,
    text: str,
) -> ApprovalNotice:
    """The notice to the requester, filled with the last verdict."""
    verdict = state.last_verdict(project_data)
    return {
        "subject": subject,
        "status": status.value,
        "text": text,
        "by": verdict.get("by"),
        "date": verdict.get("date"),
        "message": verdict.get("message"),
    }


#: What appears in the list as the SUBJECT of an approval about the service itself.
#: There is one per project, and what it is about is not a value but the service:
#: "may this project use this service".
SERVICE_USE_SUBJECT = "Gebruik van de dienst"


@dataclass(frozen=True)
class ServiceUseApproval:
    """The complete form of 'yes, this project may use this service'.

    What :func:`service_use_approval` returns.

    Attributes:
        spec: the :class:`ApprovalSpec` for ``config_approvals()`` -- read, list, record
            and notice to requester.
        is_approved: THE GATE. Everything that turns the service on hangs on this, so the
            parts never disagree.
        ensure_requested: turning on the service IS the request; this records it. State-shaped
            and thus idempotent.
    """

    spec: ApprovalSpec
    is_approved: Callable[[dict[str, Any]], bool]
    ensure_requested: Callable[[dict[str, Any]], None]


def service_use_approval(
    service_type: ServiceType,
    *,
    label: str,
    activity: str,
    consequence: str,
    approver: ApproverScope = ApproverScope.PLATFORM_ADMIN,
) -> ServiceUseApproval:
    """The boolean approval: may this project use this service, yes or no.

    One declaration produces the whole set: read status, item for the approval page, record
    verdict, notice to the requester, and the request itself.

    The state lives under the service itself, at ``services/[<service>]/config/approval``,
    with ``status`` and ``history``. There is ONE per project: what gets approved is "this
    project may use this service", not a separate value.

    Args:
        service_type: the service the approval belongs to.
        label: the heading of the request on the approver page.
        activity: what the decision is about, as the subject of a sentence, for instance
            "Het versturen van e-mail". The three sentences (denied, requested, not yet
            requested) are built from it, so they cannot drift apart.
        consequence: what it MEANS for the deployment while there is no approval, so a
            service that is on and does nothing does not stay silent about it.
        approver: who may decide.
    """
    name = service_type.value
    state = _ApprovalState(name, "approval")

    def _status_of(project_data: dict[str, Any], value: Any) -> ApprovalStatus:  # noqa: ANN401  # a project-file value
        """THE CHECK. ``value`` is not read: there is ONE approval per project."""
        return state.status(project_data)

    def _is_approved(project_data: dict[str, Any]) -> bool:
        return state.status(project_data) is ApprovalStatus.APPROVED

    def _items(project_data: dict[str, Any]) -> list[ApprovalItem]:
        """THE LIST. One item per project that asked for it."""
        approval = state.read(project_data)
        if approval is None:
            return []
        return [
            {
                "type": name,
                "subject": SERVICE_USE_SUBJECT,
                "domain": "",
                "name": project_data.get("name", ""),
                "current_status": approval.get("status", ""),
                "status": "skip",
                "history": approval.get("history", []),
            }
        ]

    def _record(project_data: dict[str, Any], item: ApprovalItem, history_entry: dict[str, Any]) -> None:
        """THE RECORD. Writes the verdict into the service's own config block."""
        state.write(project_data, {"status": item.get("status", "skip")}, history_entry)

    def _notices(project_data: dict[str, Any], deployment: dict[str, Any]) -> list[ApprovalNotice]:
        """THE NOTICE. What the owner is told while there is no approval."""
        if not state.selected(project_data):
            return []
        status = state.status(project_data)
        if status is ApprovalStatus.APPROVED:
            return []
        return [
            _notice(
                state,
                project_data,
                subject=project_data.get("name", ""),
                status=status,
                text=_notice_text(activity, consequence, status),
            )
        ]

    def _ensure_requested(project_data: dict[str, Any]) -> None:
        """Switching the service on IS the request.

        State shaped and not event shaped: read the project as it stands and fill in what is
        missing. That way a request over the API lands in the same place as a tick in the
        wizard, and running a second time changes nothing.
        """
        if not state.selected(project_data) or state.read(project_data) is not None:
            return
        state.write(project_data, {"status": ApprovalStatus.REQUESTED.value})

    return ServiceUseApproval(
        spec=ApprovalSpec(
            key=name,
            label=label,
            approver=approver,
            status_of=_status_of,
            list_items=_items,
            record=_record,
            notices_for=_notices,
        ),
        is_approved=_is_approved,
        ensure_requested=_ensure_requested,
    )


#: Suffix of the approval block of a value: ``from-address`` -> ``from-address-approval``. A
#: fixed shape, so a reader of a project file knows where the verdict on every approvable field
#: lives.
VALUE_APPROVAL_SUFFIX = "-approval"

#: Key under which the block remembers the LAST approved value. The verdict hangs on the
#: value, so a changed value is a new request; without this memory an open request would also
#: take away what already held.
LAST_APPROVED_KEY = "approved-value"


def _status_of_value(state: _ApprovalState, data: dict[str, Any], value: Any) -> ApprovalStatus:  # noqa: ANN401  # a project-file value
    """THE CHECK on a value: a verdict applies only to the value it was about."""
    block = state.read(data)
    if block is None:
        return ApprovalStatus.NONE
    if "value" in block and block.get("value") == value:
        return state.status(data)
    if LAST_APPROVED_KEY in block and block.get(LAST_APPROVED_KEY) == value:
        return ApprovalStatus.APPROVED
    return ApprovalStatus.NONE


def _record_value(
    state: _ApprovalState,
    data: dict[str, Any],
    value: Any,  # noqa: ANN401  # a project-file value
    item: ApprovalItem,
    history_entry: dict[str, Any],
) -> None:
    """THE RECORD on a value: the verdict lands with the value it was about.

    A verdict about a value other than the one now requested writes nothing: the project can
    change the field between rendering the list and submitting it.
    """
    if value is None or item.get("domain", "") != str(value):
        return
    status = item.get("status", "skip")
    fields: dict[str, Any] = {"status": status, "value": value}
    if status == ApprovalStatus.APPROVED.value:
        fields[LAST_APPROVED_KEY] = value
    state.write(data, fields, history_entry)
    if status == ApprovalStatus.DENIED.value and (state.read(data) or {}).get(LAST_APPROVED_KEY) == value:
        # Denying exactly the standing approval withdraws it. Without this an approved value
        # cannot be withdrawn. A denial of ANOTHER requested value leaves the standing approval
        # alone.
        (state.read(data) or {}).pop(LAST_APPROVED_KEY, None)


def _ensure_value_requested(state: _ApprovalState, data: dict[str, Any], value: Any) -> None:  # noqa: ANN401  # a project-file value
    """THE REQUEST on a value: setting the value IS the request."""
    if value is None:
        # A decision applies only to the state it was given for. If the block stayed when the
        # field goes back to the harmless state, that approval would apply again later without
        # a decision.
        state.clear(data)
        return
    block = state.read(data)
    last = (block or {}).get(LAST_APPROVED_KEY)
    if block is not None:
        if "value" in block and block.get("value") == value:
            return
        if "value" not in block and last == value:
            return
    # A new value starts with an empty history: the previous verdicts were about another
    # value. The last approved value DOES travel along: it applies while this request is open.
    state.clear(data)
    if last is not None and last == value:
        state.write(data, {LAST_APPROVED_KEY: last})
        return
    fields: dict[str, Any] = {"status": ApprovalStatus.REQUESTED.value, "value": value}
    if last is not None:
        fields[LAST_APPROVED_KEY] = last
    state.write(data, fields)


def _stored_approved_values(
    states: list[tuple[str, _ApprovalState]],
    data: dict[str, Any],
) -> dict[str, Any]:
    """THE MEMORY on a value: what each block has standing as approved.

    Only ``_record_value`` may make this value appear or change, and that runs on the
    approval route alone. A block holding nothing but an ``approved-value`` is a
    legitimate state too (back to the last approved value), so its mere presence says
    nothing; the COMPARISON with the previous version does. Without that comparison a
    hand-written ``approved-value`` counts as approved while no item ever stood on the
    approvals page, because ``_items`` only lists a block that carries a ``value``.

    The identity is the name within the project (empty for a project-wide value), not
    ``<project>/<name>``: within one project file that is unambiguous, and it does not
    shift when the project is renamed.
    """
    values: dict[str, Any] = {}
    for name, state in states:
        block = state.read(data) or {}
        if LAST_APPROVED_KEY in block:
            values[name] = block[LAST_APPROVED_KEY]
    return values


def _value_item(
    *,
    field: str,
    block: dict[str, Any],
    value: Any,  # noqa: ANN401  # a project-file value
    subject: str,
    name: str,
    extra: dict[str, Any] | None = None,
) -> ApprovalItem:
    """The item for the approver page, with the REQUESTED VALUE as part of its identity.

    The guard on approval decisions (``validate_approval_decisions``) pairs items on (service,
    type, domain, name), so another value is another item and an approval cannot ride along on
    a value that changed meanwhile. ``name`` carries the project, and at the deployment layer
    the deployment too.
    """
    return {
        "type": field,
        "subject": subject,
        "domain": str(value),
        "name": name,
        "current_status": block.get("status", ""),
        "status": "skip",
        "history": block.get("history", []),
        **(extra or {}),
    }


@dataclass(frozen=True)
class ConfigValueApproval:
    """The complete shape of "this VALUE only applies once somebody approves it".

    What :func:`config_value_approval` returns. The difference with
    :class:`ServiceUseApproval` is what the decision is about: not the service, but a value in
    its config, so an approval applies only to the value that was approved.

    Attributes:
        spec: the :class:`ApprovalSpec` for ``config_approvals()``.
        is_approved: THE GUARD: does the config ask for a decision, and is it there?
        approved_value: the requested value as text when it is approved, else empty.
        last_approved_value: the last value an administrator said "yes" to, also when the
            project asked for something else afterwards. Empty when nothing was ever approved.
        ensure_requested: setting the value IS the request; this records it. State shaped and
            therefore idempotent.
        record_migration_approval: writes an approved block for a value as a schema
            migration; see :func:`record_migration_approval`.
    """

    spec: ApprovalSpec
    is_approved: Callable[[dict[str, Any]], bool]
    approved_value: Callable[[dict[str, Any]], str]
    last_approved_value: Callable[[dict[str, Any]], str]
    ensure_requested: Callable[[dict[str, Any]], None]
    record_migration_approval: Callable[[dict[str, Any], Any, str], None]


def config_value_approval(
    service_type: ServiceType,
    *,
    field: str,
    label: str,
    activity: str | Callable[[Any], str],
    consequence: str | Callable[[dict[str, Any], dict[str, Any]], str],
    required_when: Callable[[dict[str, Any]], bool] | None = None,
    requested_value: Callable[[dict[str, Any]], str] | None = None,
    subject: Callable[[dict[str, Any], str], str] | None = None,
    approver: ApproverScope = ApproverScope.PLATFORM_ADMIN,
) -> ConfigValueApproval:
    """The approval of a config value: this state of this field asks for a decision.

    The counterpart of :func:`service_use_approval`. There the service itself is the subject
    and there is one decision per project; here a VALUE is the subject, only a requested value
    asks for a decision, and an approval applies to that value. Change the value and a new
    request appears.

    The state lives under the service, at ``services/[<service>]/config/<field>-approval``,
    with ``status``, the requested ``value``, the last approved ``approved-value`` and
    ``history``. Mark that field as ``PLATFORM_MANAGED`` in the config model: a project that
    may set its own status has no approval.

    Two ways to say WHAT is requested, exactly one of them:

    - ``required_when``: a predicate on the config. The value of ``field`` is the question
      while the predicate holds. Redis (``acl-key-prefix`` off) works that way.
    - ``requested_value``: the requested value as text, empty when nothing is requested. The
      service decides what "nothing requested" is: an empty field, or a number within what the
      project is already allowed. Send-email (sender address, daily budget) works that way.

    Going back to the last approved value is not a request: the block then holds only that
    value, without an open request. The status is NOT set to ``approved`` in that case,
    because a decision only comes from the approval route (``validate_approval_decisions``).

    Args:
        service_type: the service the field belongs to.
        field: the key in the service's project config the decision is about.
        label: the heading of the request on the approver page.
        activity: what the decision is about, as the subject of a sentence. A function of the
            requested value when that belongs in the sentence ("Het afzenderadres ...").
        consequence: what happens meanwhile, in the service's words. A function of project and
            deployment when it depends on them; empty is allowed.
        required_when: whether the config as it stands asks for a decision.
        requested_value: the requested value as text, empty for no request.
        subject: what the request is called on the approver page; ``activity`` by default.
        approver: who may decide.
    """
    if (required_when is None) == (requested_value is None):
        raise TypeError("config_value_approval vraagt precies een van required_when en requested_value")

    state = _ApprovalState(service_type.value, f"{field}{VALUE_APPROVAL_SUFFIX}")

    def _requested(project_data: dict[str, Any]) -> Any:  # noqa: ANN401  # a project-file value
        """The value a decision is needed about, or None when nothing is requested."""
        if requested_value is not None:
            return requested_value(project_data) or None
        config = state.service_config(project_data)
        return config.get(field) if required_when is not None and required_when(config) else None

    def _activity(value: Any) -> str:  # noqa: ANN401  # a project-file value
        return activity(value) if callable(activity) else activity

    def _consequence(project_data: dict[str, Any], deployment: dict[str, Any]) -> str:
        return consequence(project_data, deployment) if callable(consequence) else consequence

    def _last_approved(project_data: dict[str, Any]) -> Any:  # noqa: ANN401  # a project-file value
        return (state.read(project_data) or {}).get(LAST_APPROVED_KEY)

    def _status_of(project_data: dict[str, Any], value: Any) -> ApprovalStatus:  # noqa: ANN401  # a project-file value
        return _status_of_value(state, project_data, value)

    def _is_approved(project_data: dict[str, Any]) -> bool:
        """THE GUARD: is the field in a state that asks for a decision, and is it there?"""
        value = _requested(project_data)
        return value is not None and _status_of(project_data, value) is ApprovalStatus.APPROVED

    def _approved_value(project_data: dict[str, Any]) -> str:
        return str(_requested(project_data)) if _is_approved(project_data) else ""

    def _last_approved_value(project_data: dict[str, Any]) -> str:
        last = _last_approved(project_data)
        return "" if last is None else str(last)

    def _approved_values(project_data: dict[str, Any]) -> dict[str, Any]:
        """THE MEMORY. One project-wide value, under the empty identity."""
        return _stored_approved_values([("", state)], project_data)

    def _items(project_data: dict[str, Any]) -> list[ApprovalItem]:
        """THE LIST. One item while a request is open or has been judged."""
        block = state.read(project_data)
        value = _requested(project_data)
        if block is None or "value" not in block or value is None:
            return []
        return [
            _value_item(
                field=field,
                block=block,
                value=value,
                subject=subject(project_data, str(value)) if subject else _activity(value),
                name=project_data.get("name", ""),
            )
        ]

    def _record(project_data: dict[str, Any], item: ApprovalItem, history_entry: dict[str, Any]) -> None:
        """THE RECORD. The verdict lands with the value it was about."""
        _record_value(state, project_data, _requested(project_data), item, history_entry)

    def _notices(project_data: dict[str, Any], deployment: dict[str, Any]) -> list[ApprovalNotice]:
        """THE NOTICE. What the owner is told while the value does not apply."""
        value = _requested(project_data)
        if not state.selected(project_data) or value is None:
            return []
        status = _status_of(project_data, value)
        if status is ApprovalStatus.APPROVED:
            return []
        return [
            _notice(
                state,
                project_data,
                subject=_activity(value),
                status=status,
                text=_notice_text(_activity(value), _consequence(project_data, deployment), status),
            )
        ]

    def _ensure_requested(project_data: dict[str, Any]) -> None:
        """Setting the value IS the request. Another value is a new request."""
        if not state.selected(project_data):
            return
        _ensure_value_requested(state, project_data, _requested(project_data))

    return ConfigValueApproval(
        spec=ApprovalSpec(
            key=field,
            label=label,
            approver=approver,
            status_of=_status_of,
            list_items=_items,
            record=_record,
            notices_for=_notices,
            approved_values=_approved_values,
            # With ``required_when`` the config as it stands says whether enabling the service
            # is itself a request; with ``requested_value`` it never is, because only a VALUE
            # asks for a decision. Same answer as ``config_item_value_approval``.
            requires_approval_for=(
                (lambda config: required_when(config if isinstance(config, dict) else {}))
                if required_when is not None
                else lambda _config: False
            ),
        ),
        is_approved=_is_approved,
        approved_value=_approved_value,
        last_approved_value=_last_approved_value,
        ensure_requested=_ensure_requested,
        record_migration_approval=lambda project_data, value, message: record_migration_approval(
            state, project_data, value, message
        ),
    )


#: Prefix of the spec key of an approval at the DEPLOYMENT layer.
#:
#: A key of its own and not the field's, because ``Service.approval_specs`` deduplicates on
#: key: with the same key as the project request the deployment request would no longer show up
#: on the approver page at all.
DEPLOYMENT_APPROVAL_KEY_PREFIX = "deployment-"

#: Separator between project and deployment in the identity of such an item.
#:
#: A ``/`` occurs in no project name and in no deployment name, so a deployment's item never
#: coincides with the project item or with another deployment's. That identity is what
#: ``validate_approval_decisions`` pairs, and what the reseed on the approver page keeps.
DEPLOYMENT_ITEM_SEPARATOR = "/"


def deployment_item_name(project_name: str, deployment_name: str) -> str:
    """The identity of a deployment item on the approver page: ``<project>/<deployment>``."""
    return f"{project_name}{DEPLOYMENT_ITEM_SEPARATOR}{deployment_name}"


@dataclass(frozen=True)
class DeploymentValueApproval:
    """The same as :class:`ConfigValueApproval`, but per DEPLOYMENT.

    A shape of its own and not a ``layer`` flag on that dataclass: every guard here takes the
    deployment name as an extra argument, and one dataclass carrying both signatures types
    neither. The RULES are shared: both factories use ``_status_of_value``, ``_record_value``
    and ``_ensure_value_requested``, so a verdict about a value works the same on both
    layers.

    Attributes:
        spec: the :class:`ApprovalSpec` for ``config_approvals(ConfigLayer.DEPLOYMENT)``.
        is_approved: THE GUARD: does this deployment ask for a decision, and is it there?
        approved_value: this deployment's requested value when it is approved.
        last_approved_value: the last value an administrator said "yes" to for this deployment,
            also when it asked for something else afterwards.
        ensure_requested: records every deployment's request. State shaped.
        record_migration_approval: writes an approved block for ONE deployment as a schema
            migration; see :func:`record_migration_approval`.
    """

    spec: ApprovalSpec
    is_approved: Callable[[dict[str, Any], str], bool]
    approved_value: Callable[[dict[str, Any], str], str]
    last_approved_value: Callable[[dict[str, Any], str], str]
    ensure_requested: Callable[[dict[str, Any]], None]
    record_migration_approval: Callable[[dict[str, Any], str, Any, str], None]


def deployment_value_approval(
    service_type: ServiceType,
    *,
    field: str,
    label: str,
    activity: str | Callable[[Any], str],
    consequence: str | Callable[[dict[str, Any], dict[str, Any]], str],
    requested_value: Callable[[dict[str, Any], str], str],
    subject: Callable[[dict[str, Any], str, str], str] | None = None,
    approver: ApproverScope = ApproverScope.PLATFORM_ADMIN,
) -> DeploymentValueApproval:
    """The approval of a config value ONE DEPLOYMENT overrides.

    The counterpart of :func:`config_value_approval` at the deployment layer. A project sets a
    value for all its deployments; a deployment deviating from it asks for a decision of its
    own, and that decision belongs to that deployment alone. An approved deviation therefore
    keeps standing when the project value goes down later, while a deployment that INHERITS
    follows along.

    The state lives at ``deployments[*].services[<service>].config/<field>-approval``, in the
    same block shape as on the project: ``status``, the requested ``value``, the last approved
    ``approved-value`` and ``history``.

    Args:
        service_type: the service the field belongs to.
        field: the key in the service's deployment config the decision is about.
        label: the heading of the request on the approver page.
        activity: what the decision is about, as the subject of a sentence.
        consequence: what happens meanwhile, in the service's words.
        requested_value: ONE deployment's requested value as text, empty for no request. The
            service decides what "nothing requested" is: an empty field, or a value the
            project was already allowed.
        subject: what the request is called on the approver page, given project, deployment
            and value; by default ``activity`` with the deployment added.
        approver: who may decide.
    """
    state_key = f"{field}{VALUE_APPROVAL_SUFFIX}"
    service = service_type.value
    selection = _ApprovalState(service, state_key)

    def _state(deployment_name: str) -> _ApprovalState:
        return _ApprovalState(
            service, state_key, holder=lambda pd: Project(pd).find("deployments", name=deployment_name) or {}
        )

    def _deployment_names(project_data: dict[str, Any]) -> list[str]:
        return [
            deployment["name"]
            for deployment in project_data.get("deployments") or []
            if isinstance(deployment, dict) and deployment.get("name")
        ]

    def _requested(project_data: dict[str, Any], deployment_name: str) -> Any:  # noqa: ANN401  # a project-file value
        """The value a decision is needed about, or None when nothing is requested."""
        return requested_value(project_data, deployment_name) or None

    def _activity(value: Any) -> str:  # noqa: ANN401  # a project-file value
        return activity(value) if callable(activity) else activity

    def _subject(project_data: dict[str, Any], deployment_name: str, value: Any) -> str:  # noqa: ANN401  # a project-file value
        if subject is not None:
            return subject(project_data, deployment_name, str(value))
        return f"{_activity(value)} voor deployment {deployment_name}"

    def _status_of(project_data: dict[str, Any], value: Any) -> ApprovalStatus:  # noqa: ANN401  # a project-file value
        """THE CHECK, with the deployment inside ``value``: generic code does not know the layer.

        ``value`` is the pair (deployment, value) here. The generic layer treats it as an
        opaque value, exactly like publish-on-web's domain/subdomain pair.
        """
        deployment_name, requested = value
        return _status_of_value(_state(deployment_name), project_data, requested)

    def _is_approved(project_data: dict[str, Any], deployment_name: str) -> bool:
        """THE GUARD: does this deployment ask for a decision, and is it there?"""
        value = _requested(project_data, deployment_name)
        return value is not None and _status_of(project_data, (deployment_name, value)) is ApprovalStatus.APPROVED

    def _approved_value(project_data: dict[str, Any], deployment_name: str) -> str:
        if not _is_approved(project_data, deployment_name):
            return ""
        return str(_requested(project_data, deployment_name))

    def _last_approved_value(project_data: dict[str, Any], deployment_name: str) -> str:
        last = (_state(deployment_name).read(project_data) or {}).get(LAST_APPROVED_KEY)
        return "" if last is None else str(last)

    def _approved_values(project_data: dict[str, Any]) -> dict[str, Any]:
        """THE MEMORY, per deployment."""
        return _stored_approved_values([(name, _state(name)) for name in _deployment_names(project_data)], project_data)

    def _items(project_data: dict[str, Any]) -> list[ApprovalItem]:
        """THE LIST. One item per deployment with an open or judged request."""
        items: list[ApprovalItem] = []
        project_name = project_data.get("name", "")
        for deployment_name in _deployment_names(project_data):
            block = _state(deployment_name).read(project_data)
            value = _requested(project_data, deployment_name)
            if block is None or "value" not in block or value is None:
                continue
            items.append(
                _value_item(
                    field=f"{DEPLOYMENT_APPROVAL_KEY_PREFIX}{field}",
                    block=block,
                    value=value,
                    subject=_subject(project_data, deployment_name, value),
                    name=deployment_item_name(project_name, deployment_name),
                    extra={"deployment": deployment_name},
                )
            )
        return items

    def _deployment_of(item: ApprovalItem) -> str:
        """Which deployment this item is, from the identity that survives the reseed."""
        return str(item.get("name", "")).partition(DEPLOYMENT_ITEM_SEPARATOR)[2]

    def _record(project_data: dict[str, Any], item: ApprovalItem, history_entry: dict[str, Any]) -> None:
        """THE RECORD. The verdict lands with the deployment and the value it was about."""
        deployment_name = _deployment_of(item)
        if not deployment_name:
            return
        _record_value(
            _state(deployment_name),
            project_data,
            _requested(project_data, deployment_name),
            item,
            history_entry,
        )

    def _notices(project_data: dict[str, Any], deployment: dict[str, Any]) -> list[ApprovalNotice]:
        """THE NOTICE. What the owner of THIS deployment is told."""
        deployment_name = deployment.get("name") or ""
        value = _requested(project_data, deployment_name) if deployment_name else None
        if not selection.selected(project_data) or value is None:
            return []
        status = _status_of(project_data, (deployment_name, value))
        if status is ApprovalStatus.APPROVED:
            return []
        consequence_text = consequence(project_data, deployment) if callable(consequence) else consequence
        return [
            _notice(
                _state(deployment_name),
                project_data,
                subject=_subject(project_data, deployment_name, value),
                status=status,
                text=_notice_text(_activity(value), consequence_text, status),
            )
        ]

    def _ensure_requested(project_data: dict[str, Any]) -> None:
        """Setting the value IS the request, per deployment."""
        if not selection.selected(project_data):
            return
        for deployment_name in _deployment_names(project_data):
            _ensure_value_requested(
                _state(deployment_name),
                project_data,
                _requested(project_data, deployment_name),
            )

    return DeploymentValueApproval(
        spec=ApprovalSpec(
            key=f"{DEPLOYMENT_APPROVAL_KEY_PREFIX}{field}",
            label=label,
            approver=approver,
            status_of=_status_of,
            list_items=_items,
            record=_record,
            notices_for=_notices,
            approved_values=_approved_values,
            # Enabling the service asks nothing; only a deployment's own value does.
            requires_approval_for=lambda _config: False,
        ),
        is_approved=_is_approved,
        approved_value=_approved_value,
        last_approved_value=_last_approved_value,
        ensure_requested=_ensure_requested,
        record_migration_approval=lambda project_data, deployment_name, value, message: record_migration_approval(
            _state(deployment_name), project_data, value, message
        ),
    )


class _ItemApprovalState(_ApprovalState):
    """The approval block of ONE item from a list in the service's project config.

    The blocks live together under one config field keyed by item name
    (``<field>-approval: {<item>: {...}}``) instead of on the item itself. On the item the
    block would be unprotected, because ``PLATFORM_MANAGED`` only covers top-level fields
    of a config block, and a project could then write its own approval.
    """

    def __init__(self, service: str, map_key: str, item_name: str) -> None:
        super().__init__(service, item_name)
        self.map_key = map_key

    def _block_holder(self, project_data: dict[str, Any], *, create: bool) -> dict[str, Any] | None:
        """The collection of blocks, one level below the service config."""
        holder = super()._block_holder(project_data, create=create)
        if holder is None:
            return None
        blocks = holder.setdefault(self.map_key, {}) if create else holder.get(self.map_key)
        return blocks if isinstance(blocks, dict) else None

    def clear(self, project_data: dict[str, Any]) -> None:
        """Remove this item's block, and the collection once it is empty."""
        super().clear(project_data)
        config = super()._block_holder(project_data, create=False)
        if config is not None and config.get(self.map_key) == {}:
            config.pop(self.map_key, None)


@dataclass(frozen=True)
class ConfigItemValueApproval:
    """Same as :class:`ConfigValueApproval`, but per item of a list in the project config.

    Attributes:
        spec: the :class:`ApprovalSpec` for ``config_approvals(ConfigLayer.PROJECT)``.
        is_approved: THE GATE: does this item ask for a decision, and is it there?
        last_approved_value: the last value an administrator approved for this item.
        ensure_requested: records the request of every item and removes the blocks of
            items that no longer exist. State-shaped, so idempotent.
        record_migration_approval: writes an approved block for this item as a schema
            migration; see :func:`record_migration_approval`.
    """

    spec: ApprovalSpec
    is_approved: Callable[[dict[str, Any], str], bool]
    last_approved_value: Callable[[dict[str, Any], str], str]
    ensure_requested: Callable[[dict[str, Any]], None]
    record_migration_approval: Callable[[dict[str, Any], str, Any, str], None]


def config_item_value_approval(
    service_type: ServiceType,
    *,
    field: str,
    label: str,
    item_names: Callable[[dict[str, Any]], list[str]],
    activity: Callable[[str, Any], str],
    consequence: Callable[[dict[str, Any], str], str],
    requested_value: Callable[[dict[str, Any], str], str],
    subject: Callable[[dict[str, Any], str, str], str] | None = None,
    approver: ApproverScope = ApproverScope.PLATFORM_ADMIN,
) -> ConfigItemValueApproval:
    """Approval of a value on ONE item of a list in the project config.

    Where :func:`deployment_value_approval` gives a decision per deployment, this gives one
    per item, e.g. per registry of image-registries. The rules are shared
    (``_status_of_value``, ``_record_value``, ``_ensure_value_requested``); only where the
    block lives differs, see :class:`_ItemApprovalState`.

    The state lives at ``services/[<service>]/config/<field>-approval/<item>``. Mark
    ``<field>-approval`` as ``PLATFORM_MANAGED`` in the config model.

    Args:
        service_type: the service that owns the list.
        field: the key of the value on the item.
        label: the heading of the request on the approvals page.
        item_names: the names of the items the project has now.
        activity: what the decision is about, given item and value, as a sentence subject.
        consequence: what happens for this item in the meantime.
        requested_value: the requested value of ONE item as text, empty for no request.
        subject: what the request is called on the approvals page; defaults to ``activity``.
        approver: who may decide.
    """
    map_key = f"{field}{VALUE_APPROVAL_SUFFIX}"
    service = service_type.value
    #: The service itself: whether it is selected, and the collection of blocks as a whole.
    selection = _ApprovalState(service, map_key)

    def _state(item_name: str) -> _ItemApprovalState:
        return _ItemApprovalState(service, map_key, item_name)

    def _requested(project_data: dict[str, Any], item_name: str) -> Any:  # noqa: ANN401  # a project-file value
        return requested_value(project_data, item_name) or None

    def _subject(project_data: dict[str, Any], item_name: str, value: Any) -> str:  # noqa: ANN401  # a project-file value
        if subject is not None:
            return subject(project_data, item_name, str(value))
        return activity(item_name, value)

    def _status_of(project_data: dict[str, Any], value: Any) -> ApprovalStatus:  # noqa: ANN401  # a project-file value
        """The CHECK, with the item in ``value``: the pair (item, value), as per deployment."""
        item_name, item_value = value
        return _status_of_value(_state(item_name), project_data, item_value)

    def _is_approved(project_data: dict[str, Any], item_name: str) -> bool:
        value = _requested(project_data, item_name)
        return value is not None and _status_of(project_data, (item_name, value)) is ApprovalStatus.APPROVED

    def _last_approved_value(project_data: dict[str, Any], item_name: str) -> str:
        last = (_state(item_name).read(project_data) or {}).get(LAST_APPROVED_KEY)
        return "" if last is None else str(last)

    def _approved_values(project_data: dict[str, Any]) -> dict[str, Any]:
        """THE MEMORY, per STORED block, so a block of an unknown item counts too."""
        return _stored_approved_values(
            [(name, _state(name)) for name in selection.read(project_data) or {}], project_data
        )

    def _items(project_data: dict[str, Any]) -> list[ApprovalItem]:
        """The LIST. One entry per list item with an open or decided request."""
        items: list[ApprovalItem] = []
        project_name = project_data.get("name", "")
        for item_name in item_names(project_data):
            block = _state(item_name).read(project_data)
            value = _requested(project_data, item_name)
            if block is None or "value" not in block or value is None:
                continue
            items.append(
                _value_item(
                    field=field,
                    block=block,
                    value=value,
                    subject=_subject(project_data, item_name, value),
                    # Same separator as per deployment: an item name never contains a ``/``.
                    name=f"{project_name}{DEPLOYMENT_ITEM_SEPARATOR}{item_name}",
                )
            )
        return items

    def _record(project_data: dict[str, Any], item: ApprovalItem, history_entry: dict[str, Any]) -> None:
        item_name = str(item.get("name", "")).partition(DEPLOYMENT_ITEM_SEPARATOR)[2]
        if not item_name:
            return
        _record_value(_state(item_name), project_data, _requested(project_data, item_name), item, history_entry)

    def _notices(project_data: dict[str, Any], deployment: dict[str, Any]) -> list[ApprovalNotice]:
        if not selection.selected(project_data):
            return []
        notices: list[ApprovalNotice] = []
        for item_name in item_names(project_data):
            value = _requested(project_data, item_name)
            if value is None:
                continue
            status = _status_of(project_data, (item_name, value))
            if status is ApprovalStatus.APPROVED:
                continue
            notices.append(
                _notice(
                    _state(item_name),
                    project_data,
                    subject=_subject(project_data, item_name, value),
                    status=status,
                    text=_notice_text(activity(item_name, value), consequence(project_data, item_name), status),
                )
            )
        return notices

    def _ensure_requested(project_data: dict[str, Any]) -> None:
        if not selection.selected(project_data):
            return
        names = item_names(project_data)
        for item_name in names:
            _ensure_value_requested(_state(item_name), project_data, _requested(project_data, item_name))
        # A removed item's block would hand a later item with the same name a decision
        # that was not about it.
        for orphan in [name for name in selection.read(project_data) or {} if name not in names]:
            _state(orphan).clear(project_data)

    return ConfigItemValueApproval(
        spec=ApprovalSpec(
            key=field,
            label=label,
            approver=approver,
            status_of=_status_of,
            list_items=_items,
            record=_record,
            notices_for=_notices,
            approved_values=_approved_values,
            # Enabling the service asks nothing; only a value above the free range does.
            requires_approval_for=lambda _config: False,
        ),
        is_approved=_is_approved,
        last_approved_value=_last_approved_value,
        ensure_requested=_ensure_requested,
        record_migration_approval=lambda project_data, item, value, message: record_migration_approval(
            _state(item), project_data, value, message
        ),
    )
