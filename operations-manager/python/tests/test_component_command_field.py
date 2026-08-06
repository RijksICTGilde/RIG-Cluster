"""The component's start command is settable, optional, and absent when empty.

`command` already existed in the schema (on both the component and the
deployment-component) and the manifest already rendered it, but there was no editable, no
form field and no API field: the only way to set it was hand-editing the project file,
while the schema suggested it was supported.

It is a sharp tool. Kubernetes replaces the image's ENTRYPOINT with this, so a value here
silently discards the image's own start-up logic, and a command the image does not carry
gives a pod that never starts with an error that points nowhere -- our own test images hit
`exec: "sh": executable file not found in $PATH`. Hence the warning in the help text and
the insistence on it staying out of the file when empty.
"""

from __future__ import annotations

from typing import Any

from opi.forms.editables.fields.components import COMPONENT_COMMAND_EDITABLE
from opi.forms.visualizers.fields.components import COMPONENT_COMMAND, COMPONENTS_SEQUENCE


def _paths(visualizer: Any) -> list[str]:
    return [child.editable.yaml_path for child in visualizer.children or []]


def test_the_field_is_offered_on_the_component() -> None:
    assert any("command" in path for path in _paths(COMPONENTS_SEQUENCE))


def test_it_is_not_required() -> None:
    """Most components should never touch this."""
    assert COMPONENT_COMMAND_EDITABLE.required is False
    assert COMPONENT_COMMAND_EDITABLE.min_items == 0


def test_empty_is_removed_rather_than_written_as_an_empty_list() -> None:
    """The schema demands ``minItems: 1``, so an empty list is not merely ugly but
    invalid, and it would also override the image's entrypoint with nothing."""
    assert COMPONENT_COMMAND_EDITABLE.remove_when_none is True


def test_the_help_text_warns_about_replacing_the_entrypoint() -> None:
    """The danger is not that a command can be wrong, it is that a correct-looking one
    discards what the image brought along. Say that, not just "be careful"."""
    help_text = (COMPONENT_COMMAND.help_text or "").lower()

    assert "leeg" in help_text, "it must say that leaving it empty is the normal case"
    assert "vervangt" in help_text, "it must say the image's own start command is replaced"


def test_the_help_text_carries_no_angle_brackets() -> None:
    """Help text becomes a ROOS attribute, and ROOS re-emits attribute values, so anything
    needing escaping is escaped twice and the reader sees the entities."""
    assert "<" not in (COMPONENT_COMMAND.help_text or "")
    assert ">" not in (COMPONENT_COMMAND.help_text or "")


def test_each_argument_is_its_own_entry() -> None:
    """A command is a list in Kubernetes; one text field with spaces in it would quietly
    turn `sh -c "x y"` into a single argument."""
    assert COMPONENT_COMMAND_EDITABLE.children
    assert COMPONENT_COMMAND_EDITABLE.children[0].yaml_path.endswith("command[*]")
