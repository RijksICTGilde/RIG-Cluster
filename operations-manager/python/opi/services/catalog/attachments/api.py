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
  (DEFINE + USE + BIND), because doing it in two calls can half-succeed.

Each field points at the shared ``Editable`` that already defines what a valid value looks
like, exactly as ``opi/api/validation.py`` does for the component endpoints. The one field
without an editable is the file itself, and it says why.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import ValidationError

from opi.services.catalog.actions import (
    ActionContext,
    ActionField,
    ActionFieldKind,
    ActionResult,
    ActionVerb,
    FieldCombination,
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

_VERB_CONTRACT = """
POST adds and refuses an id that is already taken (409). PUT updates an attachment that
exists and refuses one that does not (404). PUT with `upsert=true` writes either way and
replaces what is there, on id, without asking -- which is why it has to be asked for.
""".strip()


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
    """
    from opi.manager.project_manager import ProjectManager

    upload = ctx.values["file"]
    attachment_id = ctx.item_id or ctx.values.get("attachment_id")
    if not attachment_id:
        return ActionResult(422, {"detail": "attachment_id is verplicht"})
    if len(upload.content) > MAX_ATTACHMENT_BYTES:
        return ActionResult(413, {"detail": f"Bestand te groot (max {MAX_ATTACHMENT_KB} KB)"})
    if not upload.content:
        return ActionResult(422, {"detail": "Leeg bestand geupload"})

    project_manager = ProjectManager(project_file_relative_path=f"projects/{ctx.project_name}.yaml")
    try:
        result = await project_manager.upsert_attachment(
            attachment_id,
            upload.filename,
            upload.content,
            on_existing=ctx.verb.on_existing,
            on_absent=ctx.verb.on_absent,
            component_name=ctx.component_name,
            binding=binding,
        )
    finally:
        await project_manager.close()

    if result["success"]:
        return ActionResult(
            200 if result["replaced"] else 201,
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


async def _define_attachment(ctx: ActionContext) -> ActionResult:
    """Project-level upload: put the file in the catalog and nothing else."""
    return await _store(ctx, binding=None)


async def _define_and_bind_attachment(ctx: ActionContext) -> ActionResult:
    """Component-level upload: the same, plus the component's use of it."""
    binding = _binding_from(ctx.values)
    try:
        # The combination rule (a file needs a path, an env-var needs a name) is the
        # model's, and this is that same model -- validated here only so the caller is
        # told at once instead of by the save that follows.
        AttachmentUse.model_validate({"reference": ctx.item_id or ctx.values.get("attachment_id"), **binding})
    except ValidationError as e:
        return ActionResult(422, {"detail": "; ".join(error["msg"] for error in e.errors())})
    return await _store(ctx, binding=binding)


PROJECT_ATTACHMENT_ACTION = ServiceAction(
    action_id="attachments",
    layer=ConfigLayer.PROJECT,
    roles=(ConfigRole.DEFINE,),
    id_param="attachment_id",
    summary="Upload an attachment to the project catalog",
    description=(
        "Put a file in the project's attachments catalog. This is the define side of the service: "
        "the attachment exists in the project and is used by nothing until a component references "
        "it (see the component-level upload, or the component config endpoint).\n\n"
        f"{_VERB_CONTRACT}\n\n"
        f"The file is at most {MAX_ATTACHMENT_KB} KB and is AGE-encrypted with the project's own "
        "key before it is stored."
    ),
    fields=(_ID_FIELD, _FILE_FIELD),
    verbs=_VERBS,
    handler=_define_attachment,
    example=(
        "curl -X POST -H 'X-API-Key: <key>' "
        "-F attachment_id=server-cert -F file=@server.pem "
        "https://<host>/api/v2/projects/my-project/services/attachments/attachments"
    ),
)

COMPONENT_ATTACHMENT_ACTION = ServiceAction(
    action_id="attachments",
    layer=ConfigLayer.COMPONENT,
    roles=(ConfigRole.DEFINE, ConfigRole.USE, ConfigRole.BIND),
    id_param="attachment_id",
    summary="Upload an attachment and couple it to a component in one request",
    description=(
        "Define, use and bind in a single call: the file lands in the project's catalog, the "
        "component starts using it, and the coupling says how it reaches the pod. Doing this in "
        "two requests is possible (upload at project level, then configure the component) but can "
        "half-succeed, leaving a catalog entry nothing uses.\n\n"
        f"{_VERB_CONTRACT}\n\n"
        "An existing coupling of the same attachment on this component is replaced; couplings of "
        "other attachments are left alone."
    ),
    fields=(_ID_FIELD, _FILE_FIELD, _PROVIDE_AS_FIELD, _PATH_FIELD, _ENV_NAME_FIELD),
    verbs=_VERBS,
    combinations=(
        FieldCombination(when="provide-as=file", requires=("path",), enforced_by=_DESTINATION_RULE),
        FieldCombination(when="provide-as=env-var", requires=("env-name",), enforced_by=_DESTINATION_RULE),
    ),
    handler=_define_and_bind_attachment,
    example=(
        "curl -X POST -H 'X-API-Key: <key>' "
        "-F attachment_id=server-cert -F file=@server.pem "
        "-F provide-as=file -F path=/etc/ssl/certs/server.pem "
        "https://<host>/api/v2/projects/my-project/services/attachments/component/backend/attachments"
    ),
)

ATTACHMENT_ACTIONS = [PROJECT_ATTACHMENT_ACTION, COMPONENT_ATTACHMENT_ACTION]
