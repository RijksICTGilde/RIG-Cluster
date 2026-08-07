"""The API actions the attachments service declares for itself (RC-38).

Everything an API client needs in order to *define* an attachment and to *use and bind*
one lives here, next to the service's model, editables and templates -- the RC-36 rule
that all of a service is in its own directory.

Why this exists at all. The generic config endpoints are generated from the editables a
service declares, which is the right default and covers the rest of the catalog. It could
not cover this: an attachment's content is a file, it arrives as multipart, and it belongs
to the catalog rather than to any component's config block. The result was an API that let
a client point a component at attachment ``my-cert`` while giving it no way to put
``my-cert`` in the project. Two actions close that:

* project level -- put a file in the catalog (DEFINE);
* component level -- put a file in the catalog *and* couple it in the same request
  (DEFINE + USE + BIND), because doing it in two calls can half-succeed; or, with a
  ``reference`` instead of a file, couple an attachment that is already in the catalog
  (USE + BIND) and leave its content alone.

Each field points at the shared ``Editable`` that already defines what a valid value looks
like, exactly as ``opi/api/validation.py`` does for the component endpoints. The one field
without an editable is the file itself, and it says why.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any

from pydantic import ValidationError

from opi.services.catalog.actions import (
    ActionContext,
    ActionField,
    ActionFieldKind,
    ActionFlag,
    ActionResult,
    ActionVerb,
    FieldCombination,
    FieldDisjunction,
    ServiceAction,
)
from opi.services.catalog.attachments.catalog_model import MAX_ATTACHMENT_BYTES, MAX_ATTACHMENT_KB
from opi.services.catalog.attachments.config_model import AttachmentUse
from opi.services.catalog.attachments.editables import (
    ATTACHMENT_ID_EDITABLE,
    ATTACHMENT_USE_ENV_NAME_EDITABLE,
    ATTACHMENT_USE_PATH_EDITABLE,
    ATTACHMENT_USE_PROVIDE_AS_EDITABLE,
)
from opi.services.catalog.base import ConfigLayer, ConfigRole

logger = logging.getLogger(__name__)

#: The verbs both actions support, and what each one promises about an id that is taken.
_VERBS = (ActionVerb.CREATE, ActionVerb.UPDATE, ActionVerb.UPSERT)

#: The name of the acknowledgement that lets a delete proceed on an attachment that is
#: still in use. Named for what the caller *states* rather than for what it overrides:
#: "delete when in use" would describe the effect while saying nothing about the caller
#: having seen the consequences, and "force" says only that something was overruled. The
#: caller is confirming a fact they were told in the 409 -- same reasoning as ``rollout``.
CONFIRM_IN_USE = "confirm_in_use"

_CONFIRM_IN_USE_FLAG = ActionFlag(
    name=CONFIRM_IN_USE,
    description=(
        "Delete the attachment even though components still use it, and remove those couplings "
        "in the same change. Off by default: an attachment that is in use is refused with 409 and "
        "the list of places using it, so this is set by a caller who has seen that list. An "
        "attachment serving as a publish-on-web certificate is refused even with this set -- "
        "change the TLS mode there first."
    ),
    verbs=(ActionVerb.DELETE,),
)

_ID_FIELD = ActionField(
    name="attachment_id",
    description=(
        "Identifier for this attachment within the project; a component references it by this "
        "id. Lowercase letters, digits and dashes, starting with a letter, at most 40 characters. "
        "Supplied as a field when creating; part of the path when updating or upserting."
    ),
    editable=ATTACHMENT_ID_EDITABLE,
    required_for=(ActionVerb.CREATE,),
    example="server-cert",
)

_FILE_FIELD = ActionField(
    name="file",
    description=(
        f"The file itself, as multipart upload, at most {MAX_ATTACHMENT_KB} KB. It is encrypted "
        "with the project's own AGE key before it is stored; its name is kept as the attachment's "
        "filename."
    ),
    no_editable_reason=(
        "A file's bytes are not a form field and no Editable validates them. The one rule that "
        f"applies is its size, and that lives once in catalog_model.MAX_ATTACHMENT_BYTES "
        f"({MAX_ATTACHMENT_BYTES} bytes), enforced on every road in -- this action and the wizard "
        "upload."
    ),
    kind=ActionFieldKind.FILE,
    required_for=_VERBS,
)

#: On a component the id names *new* content, and new content is one of two ways in, so
#: it is required together with the file rather than always (see ``_SOURCE_RULE``).
_COMPONENT_ID_FIELD = replace(
    _ID_FIELD,
    required_for=(),
    description=(
        "Identifier to store the uploaded file under; a component references it by this id. "
        "Lowercase letters, digits and dashes, starting with a letter, at most 40 characters. "
        "Required together with 'file', left out when 'reference' names an attachment that already "
        "exists, and part of the path when updating or upserting."
    ),
)

_OPTIONAL_FILE_FIELD = replace(_FILE_FIELD, required_for=())

_REFERENCE_FIELD = ActionField(
    name="reference",
    description=(
        "Id of an attachment that is already in the project's catalog. Give this instead of a file "
        "to couple what is already there; the catalog entry is left untouched."
    ),
    editable=ATTACHMENT_ID_EDITABLE,
    addressed_by_path=True,
    example="server-cert",
)

_PROVIDE_AS_FIELD = ActionField(
    name="provide-as",
    description=(
        "How the component receives the attachment: 'file' mounts it at 'path', 'env-var' puts its "
        "content in the environment variable named by 'env-name'."
    ),
    editable=ATTACHMENT_USE_PROVIDE_AS_EDITABLE,
    required_for=_VERBS,
    example="file",
)

_PATH_FIELD = ActionField(
    name="path",
    description="Absolute path to mount the attachment at. Required when 'provide-as' is 'file'.",
    editable=ATTACHMENT_USE_PATH_EDITABLE,
    example="/etc/ssl/certs/server.pem",
)

_ENV_NAME_FIELD = ActionField(
    name="env-name",
    description="Environment variable to put the content in. Required when 'provide-as' is 'env-var'.",
    editable=ATTACHMENT_USE_ENV_NAME_EDITABLE,
    example="SERVER_CERT",
)

#: Where the "a delivery needs its destination" rule actually lives. Named, not repeated.
_DESTINATION_RULE = "opi.services.catalog.attachments.config_model.AttachmentUse"

#: Where the "content or reference" rule actually lives. One function, one refusal.
_SOURCE_RULE = "opi.services.catalog.attachments.api.check_attachment_source"

_VERB_CONTRACT = """
POST adds and refuses an id that is already taken (409). PUT updates an attachment that
exists and refuses one that does not (404). PUT with `upsert=true` writes either way and
replaces what is there, on id, without asking -- which is why it has to be asked for.
""".strip()

_DELETE_CONTRACT = f"""
DELETE removes the attachment and refuses an id that is not there (404). An attachment
nothing uses goes without further ado. One that is in use is refused with 409 and a
`used_by` list naming every component (and deployment, where the use sits on a
deployment component) that references it, so the caller can show what would break.
`{CONFIRM_IN_USE}=true` says that list was seen and the attachment should go anyway: the
couplings are then removed together with the catalog entry, in one change, and the
response reports them as `uncoupled_from`. An attachment used as a publish-on-web
certificate stays refused even then -- removing that reference means deciding how the
site is served instead, which is a separate change.
""".strip()


def check_attachment_source(values: dict[str, Any], *, addressed: bool) -> str | None:
    """Refuse a component request that does not say where the attachment comes from.

    The one place the "content or reference" rule is decided, which is why
    the disjunction points here instead of restating it. Two ways to get it
    wrong, and both have to be named: sending neither leaves nothing to couple, and
    sending both means the caller asked to upload a file *and* to leave the catalog
    alone.

    ``addressed`` says the route already names the attachment in its path (PUT), and then
    there is no choice to make: the path is the reference, and a file, if given, replaces
    its content.
    """
    if addressed:
        return None
    has_file = values.get("file") is not None
    has_reference = bool(values.get("reference"))
    if has_file and has_reference:
        return "Geef een 'file' of een 'reference', niet allebei"
    if not has_file and not has_reference:
        return "Geef een 'file' (nieuwe inhoud) of een 'reference' (bestaande bijlage)"
    if has_file and not values.get("attachment_id"):
        return "Een 'file' heeft een 'attachment_id' nodig om onder op te slaan"
    return None


def _binding_from(values: dict[str, Any]) -> dict[str, Any]:
    """The coupling fields of a request, as they are stored on the component."""
    binding: dict[str, Any] = {"provide-as": values.get("provide-as")}
    if values.get("path"):
        binding["path"] = values["path"]
    if values.get("env-name"):
        binding["env-name"] = values["env-name"]
    return binding


async def _store(ctx: ActionContext, binding: dict[str, Any] | None) -> ActionResult:
    """Encrypt and store one attachment, optionally coupling it to a component.

    Shared by both actions: coupling or not is the only difference between them, and the
    id semantics per verb are carried by the verb itself.

    Without a file the request referenced an attachment that is already in the catalog, so
    there is nothing to encrypt and nothing to store -- only the component's use of it is
    written, and the catalog entry it points at has to be there.
    """
    from opi.manager.project_manager import ProjectManager

    upload = ctx.values.get("file")
    attachment_id = ctx.item_id or ctx.values.get("attachment_id") or ctx.values.get("reference")
    if not attachment_id:
        return ActionResult(422, {"detail": "attachment_id is verplicht"})
    if upload is not None:
        if len(upload.content) > MAX_ATTACHMENT_BYTES:
            return ActionResult(413, {"detail": f"Bestand te groot (max {MAX_ATTACHMENT_KB} KB)"})
        if not upload.content:
            return ActionResult(422, {"detail": "Leeg bestand geupload"})

    project_manager = ProjectManager(project_file_relative_path=f"projects/{ctx.project_name}.yaml")
    try:
        result = await project_manager.upsert_attachment(
            attachment_id,
            upload.filename if upload else None,
            upload.content if upload else None,
            on_existing=ctx.verb.on_existing,
            on_absent=ctx.verb.on_absent,
            component_name=ctx.component_name,
            binding=binding,
        )
    finally:
        await project_manager.close()

    if result["success"]:
        return ActionResult(
            200 if result["replaced"] or upload is None else 201,
            {
                "attachment": result["attachment"],
                "replaced": result["replaced"],
                "component": result.get("component"),
            },
        )
    status = {"conflict": 409, "not_found": 404, "validation_error": 422, "no_encryption_key": 409}.get(
        result.get("error_type", ""), 500
    )
    return ActionResult(status, {"detail": result["error"]})


async def _remove_attachment(ctx: ActionContext) -> ActionResult:
    """Take one attachment out of the project's catalog.

    The counterpart the catalog never had: files could be added and replaced but not
    removed, so they accumulated with no way back. Two answers, and which one you get is
    decided by the project rather than by the request:

    * nothing uses it -- it goes, and that needs no confirmation;
    * something uses it -- 409 with ``used_by``, naming every place, so the caller learns
      what the deletion would break instead of only that it was refused. That is a state
      conflict, not a malformed request, which is why it is a 409 and not a 422.

    With ``confirm_in_use`` the caller states they have seen that list, and the couplings
    are removed together with the catalog entry in a single save -- half a deletion would
    leave a reference pointing at an id that no longer exists.
    """
    from opi.manager.project_manager import ProjectManager

    attachment_id = ctx.item_id
    if not attachment_id:  # pragma: no cover - the route always addresses one item
        return ActionResult(422, {"detail": "attachment_id is verplicht"})

    project_manager = ProjectManager(project_file_relative_path=f"projects/{ctx.project_name}.yaml")
    try:
        result = await project_manager.remove_attachment(
            attachment_id, confirm_in_use=ctx.flags.get(CONFIRM_IN_USE, False)
        )
    finally:
        await project_manager.close()

    if result["success"]:
        if not result["changed"]:
            # The catalog is idempotent about this, but the route addresses one named
            # attachment: reporting success for an id this project never had would tell
            # the caller their id was right.
            return ActionResult(404, {"detail": f"Bijlage '{attachment_id}' bestaat niet in dit project"})
        return ActionResult(200, {"attachment": attachment_id, "uncoupled_from": result.get("uncoupled_from", [])})

    if result.get("error_type") == "in_use":
        return ActionResult(409, {"detail": result["error"], "used_by": result.get("used_by", [])})
    status = {"not_found": 404, "validation_error": 422}.get(result.get("error_type", ""), 500)
    return ActionResult(status, {"detail": result["error"]})


async def _define_attachment(ctx: ActionContext) -> ActionResult:
    """Project-level: put the file in the catalog, or take one out of it."""
    if ctx.verb is ActionVerb.DELETE:
        return await _remove_attachment(ctx)
    return await _store(ctx, binding=None)


async def _define_and_bind_attachment(ctx: ActionContext) -> ActionResult:
    """Component-level: couple an attachment to the component, with or without new content.

    With a file the attachment is (re)defined and coupled in one call; with a reference
    only the coupling is written and the catalog entry stays as it is. Which of the two it
    is has to be unambiguous, and that is the one question answered before anything else.
    """
    source_error = check_attachment_source(ctx.values, addressed=ctx.verb.targets_existing)
    if source_error:
        return ActionResult(422, {"detail": source_error})
    binding = _binding_from(ctx.values)
    reference = ctx.item_id or ctx.values.get("attachment_id") or ctx.values.get("reference")
    try:
        # The combination rule (a file needs a path, an env-var needs a name) is the
        # model's, and this is that same model -- validated here only so the caller is
        # told at once instead of by the save that follows.
        AttachmentUse.model_validate({"reference": reference, **binding})
    except ValidationError as e:
        return ActionResult(422, {"detail": "; ".join(error["msg"] for error in e.errors())})
    return await _store(ctx, binding=binding)


PROJECT_ATTACHMENT_ACTION = ServiceAction(
    action_id="attachment",
    layer=ConfigLayer.PROJECT,
    roles=(ConfigRole.DEFINE,),
    id_param="attachment_id",
    summary="Manage attachments in the project catalog",
    description=(
        "Put a file in the project's attachments catalog, or take one out of it. This is the "
        "define side of the service: the attachment exists in the project and is used by nothing "
        "until a component references it (see the component-level upload, or the component config "
        "endpoint).\n\n"
        f"{_VERB_CONTRACT}\n\n"
        f"{_DELETE_CONTRACT}\n\n"
        f"An uploaded file is at most {MAX_ATTACHMENT_KB} KB and is AGE-encrypted with the "
        "project's own key before it is stored."
    ),
    fields=(_ID_FIELD, _FILE_FIELD),
    verbs=(*_VERBS, ActionVerb.DELETE),
    flags=(_CONFIRM_IN_USE_FLAG,),
    handler=_define_attachment,
    example=(
        "curl -X POST -H 'X-API-Key: <key>' "
        "-F attachment_id=server-cert -F file=@server.pem "
        "https://<host>/api/v2/projects/my-project/services/attachments/attachment"
    ),
    verb_examples=(
        (
            ActionVerb.DELETE,
            "curl -X DELETE -H 'X-API-Key: <key>' "
            "'https://<host>/api/v2/projects/my-project/services/attachments/attachment/server-cert"
            f"?{CONFIRM_IN_USE}=true'",
        ),
    ),
)

COMPONENT_ATTACHMENT_ACTION = ServiceAction(
    action_id="attachment",
    layer=ConfigLayer.COMPONENT,
    roles=(ConfigRole.DEFINE, ConfigRole.USE, ConfigRole.BIND),
    id_param="attachment_id",
    summary="Couple an attachment to a component, with new content or by reference",
    description=(
        "Define, use and bind in a single call: with a `file` the content lands in the project's "
        "catalog under `attachment_id`, the component starts using it, and the coupling says how it "
        "reaches the pod. Doing that in two requests is possible (upload at project level, then "
        "configure the component) but can half-succeed, leaving a catalog entry nothing uses.\n\n"
        "With a `reference` instead of a `file` only the coupling is written: the named attachment "
        "is already in the catalog and its content is left exactly as it is. One or the other -- "
        "sending both asks for two different things at once, sending neither leaves nothing to "
        "couple. On a PUT the path already names the attachment, so a `file` there replaces its "
        "content and leaving it out only rewrites the coupling.\n\n"
        f"{_VERB_CONTRACT}\n\n"
        "An existing coupling of the same attachment on this component is replaced; couplings of "
        "other attachments are left alone."
    ),
    fields=(
        _COMPONENT_ID_FIELD,
        _OPTIONAL_FILE_FIELD,
        _REFERENCE_FIELD,
        _PROVIDE_AS_FIELD,
        _PATH_FIELD,
        _ENV_NAME_FIELD,
    ),
    verbs=_VERBS,
    combinations=(
        FieldCombination(when="provide-as=file", requires=("path",), enforced_by=_DESTINATION_RULE),
        FieldCombination(when="provide-as=env-var", requires=("env-name",), enforced_by=_DESTINATION_RULE),
        FieldCombination(when="file", requires=("attachment_id",), enforced_by=_SOURCE_RULE),
    ),
    disjunctions=(
        FieldDisjunction(
            one_of=("file", "reference"),
            describes="new content for the catalog, or an attachment that is already in it",
            enforced_by=_SOURCE_RULE,
        ),
    ),
    handler=_define_and_bind_attachment,
    example=(
        "curl -X POST -H 'X-API-Key: <key>' "
        "-F attachment_id=server-cert -F file=@server.pem "
        "-F provide-as=file -F path=/etc/ssl/certs/server.pem "
        "https://<host>/api/v2/projects/my-project/services/attachments/component/backend/attachment"
    ),
)

ATTACHMENT_ACTIONS = [PROJECT_ATTACHMENT_ACTION, COMPONENT_ATTACHMENT_ACTION]
