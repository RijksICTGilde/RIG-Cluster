"""Editables for the admin domain/subdomain approval flow.

These fields target a transient ``_approval_items`` list that is populated
at wizard init time and mapped back to the real project YAML structure
by the section's ``post_merge`` callback.
"""

from __future__ import annotations

from opi.forms.editables.editable import Editable

# ---------------------------------------------------------------------------
# Individual approval-item fields (children of sequence item)
# ---------------------------------------------------------------------------

APPROVAL_ITEM_TYPE_EDITABLE = Editable(
    yaml_path="_approval_items[*]/type",
)

APPROVAL_ITEM_DOMAIN_EDITABLE = Editable(
    yaml_path="_approval_items[*]/domain",
)

APPROVAL_ITEM_NAME_EDITABLE = Editable(
    yaml_path="_approval_items[*]/name",
)

APPROVAL_ITEM_CURRENT_STATUS_EDITABLE = Editable(
    yaml_path="_approval_items[*]/current_status",
)

APPROVAL_ITEM_STATUS_EDITABLE = Editable(
    yaml_path="_approval_items[*]/status",
    required=True,
    default="skip",
    values_provider="ApprovalStatusOptionsProvider",
)

APPROVAL_ITEM_MESSAGE_EDITABLE = Editable(
    yaml_path="_approval_items[*]/message",
    remove_when_none=True,
)

# ---------------------------------------------------------------------------
# Sequence root
# ---------------------------------------------------------------------------

APPROVAL_ITEMS_EDITABLE = Editable(
    yaml_path="_approval_items",
    children=[
        APPROVAL_ITEM_TYPE_EDITABLE,
        APPROVAL_ITEM_DOMAIN_EDITABLE,
        APPROVAL_ITEM_NAME_EDITABLE,
        APPROVAL_ITEM_CURRENT_STATUS_EDITABLE,
        APPROVAL_ITEM_STATUS_EDITABLE,
        APPROVAL_ITEM_MESSAGE_EDITABLE,
    ],
)
