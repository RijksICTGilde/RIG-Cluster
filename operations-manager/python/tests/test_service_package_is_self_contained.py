"""Everything a service is, lives in that service's own directory (RC-36).

The measure is not "is it tidily divided" but: **can you copy a directory, rename it,
and does it then work the way you expect?** That only holds when nothing about a
service is written down anywhere else -- no metadata block in a shared list, no
variables enum in a shared module, no explanation in a shared template folder. Those
three used to sit outside the package, so adding a service meant editing shared files
and removing one left a hole behind.

This is the guard that keeps it that way. It is precisely the kind of property that
seeps away quietly: one service declared "just here for now" in a shared file breaks
nothing today, and a year later there are two patterns to know.

What deliberately stays shared: the ``ServiceType`` enum, the hook points, the
``Service`` base contract and the registry that mounts the packages. Those are the
vocabulary services are expressed in, not properties of one service -- and
``ServiceType`` is also what ties everything together, so a service declaring its own
member would be an import cycle.
"""

from __future__ import annotations

import inspect
import pathlib
import re

import opi
import pytest
from opi.services.registry import SERVICES, get_service
from opi.services.services import ServiceAdapter
from opi.services.services_enums import ServiceType

_OPI_ROOT = pathlib.Path(opi.__file__).parent
_CATALOG_ROOT = _OPI_ROOT / "services" / "catalog"
_SHARED_HELP_DIR = _OPI_ROOT / "templates" / "help"

_ALL_SERVICES = sorted(ServiceType, key=lambda s: s.value)


def _package_dir(service: ServiceType) -> pathlib.Path:
    """The directory holding the service's own module."""
    return pathlib.Path(inspect.getfile(type(get_service(service)))).parent


def _shared_python_files() -> list[pathlib.Path]:
    """Every OPI module that is NOT part of a service package."""
    return [path for path in _OPI_ROOT.rglob("*.py") if not path.is_relative_to(_CATALOG_ROOT)]


# ---------------------------------------------------------------------------
# Each service delivers its own definition
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("service", _ALL_SERVICES, ids=lambda s: s.value)
def test_every_service_declares_its_own_definition(service: ServiceType) -> None:
    """Not inherited, not looked up in a shared table -- declared by the class itself."""
    service_class = type(get_service(service))

    assert "definition" in service_class.__dict__, (
        f"{service_class.__name__} does not declare its own ServiceDefinition. "
        f"A service's metadata belongs in its own package, not in a shared list."
    )


@pytest.mark.parametrize("service", _ALL_SERVICES, ids=lambda s: s.value)
def test_the_definition_of_a_service_lives_inside_the_service_catalog(service: ServiceType) -> None:
    package_dir = _package_dir(service)

    assert package_dir.is_relative_to(_CATALOG_ROOT), (
        f"{service.value} is defined in {package_dir}, outside the service catalog"
    )


def test_the_assembled_registry_is_exactly_what_the_services_declare() -> None:
    """``SERVICE_DEFINITIONS`` is derived, not a second source of truth."""
    for service in ServiceType:
        assert ServiceAdapter.SERVICE_DEFINITIONS[service] is SERVICES[service].definition, (
            f"the assembled definition for {service.value} is not the object its package declares"
        )

    assert list(ServiceAdapter.SERVICE_DEFINITIONS) == list(ServiceType), (
        "the definition order must stay pinned to the ServiceType order -- it is visible "
        "in the backup labels and in the service picker"
    )


def test_a_service_without_a_definition_is_refused() -> None:
    """The guarantee is enforced at class-creation time, not only measured here."""
    from opi.services.catalog.base import Service

    with pytest.raises(TypeError, match="no 'definition'"):
        type("ServiceWithoutDefinition", (Service,), {"service_type": ServiceType.KEYCLOAK})


# ---------------------------------------------------------------------------
# No shared file carries a piece of one service
# ---------------------------------------------------------------------------


def test_no_shared_module_builds_a_service_definition() -> None:
    """A ``ServiceDefinition(...)`` outside a service package is a service described
    somewhere other than its own directory -- exactly what this PR removed."""
    offenders = [
        str(path.relative_to(_OPI_ROOT))
        for path in _shared_python_files()
        if re.search(r"\bServiceDefinition\(", path.read_text(encoding="utf-8"))
    ]

    assert offenders == [], (
        f"these shared modules declare a service's metadata: {offenders}. "
        f"A ServiceDefinition belongs in the __init__.py of the service it describes."
    )


def test_no_shared_module_declares_the_variables_of_a_service() -> None:
    """The variables a service hands to a deployment are part of that service."""
    offenders = [
        str(path.relative_to(_OPI_ROOT))
        for path in _shared_python_files()
        if re.search(r"\bVariableDefinition\(", path.read_text(encoding="utf-8"))
    ]

    assert offenders == [], (
        f"these shared modules declare service variables: {offenders}. "
        f"Put the enum in the package of the service that provides them (variables.py)."
    )


@pytest.mark.parametrize("service", _ALL_SERVICES, ids=lambda s: s.value)
def test_the_explanation_of_a_service_sits_next_to_its_other_templates(service: ServiceType) -> None:
    """``section-detail.html.j2`` was already in the package; the explanation is a
    template of the same service, so a second location for it was one too many."""
    definition = ServiceAdapter.get_service_definition(service)
    assert definition.help_template is not None

    help_file = _CATALOG_ROOT / definition.help_template
    assert help_file.is_file(), f"{service.value} points at {definition.help_template}, which is not in the catalog"
    assert help_file.parent == _package_dir(service), (
        f"the explanation of {service.value} lives in {help_file.parent}, not in its own package"
    )


def test_the_shared_help_folder_holds_no_service_explanation() -> None:
    """What is left there belongs to no single service (the container-image note)."""
    service_help_files = {
        (_CATALOG_ROOT / d.help_template).name for d in ServiceAdapter.SERVICE_DEFINITIONS.values() if d.help_template
    }
    leftovers = sorted(path.name for path in _SHARED_HELP_DIR.glob("*.html.j2"))

    assert service_help_files == {"help.html.j2"}
    for name in leftovers:
        assert name != "help.html.j2", f"{name} looks like a service explanation left behind in templates/help"
