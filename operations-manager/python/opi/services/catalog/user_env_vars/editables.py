"""Editables owned by the ``user-env-vars`` system service.

Two layers, one field each, and both yaml_paths are plain component properties rather
than ``config_path`` blocks inside the ``services`` list: this service owns
``components[*]/user-env-vars`` and ``deployments[*]/components[*]/user-env-vars``. Being a
service gives them the config model, the schema fragment, the validator and the declared
form sections; the data stays where it is.

The deployment-component value wins over the component value on a collision (merged in
``ProjectManager``), which is why the same service carries both.
"""

from __future__ import annotations

from opi.forms.editables.converters import KeyValueConverter
from opi.forms.editables.editable import Editable
from opi.forms.editables.validators import KeyValueValidator

COMPONENT_USER_ENV_VARS_EDITABLE = Editable(
    yaml_path="components[*]/user-env-vars",
    converter=KeyValueConverter(fmt="env", write_as="string"),
    validator=KeyValueValidator(),
    remove_when_none=True,
)

DEPLOYMENT_COMP_USER_ENV_VARS_EDITABLE = Editable(
    yaml_path="deployments[*]/components[*]/user-env-vars",
    converter=KeyValueConverter(fmt="env", write_as="string"),
    validator=KeyValueValidator(),
    remove_when_none=True,
)
