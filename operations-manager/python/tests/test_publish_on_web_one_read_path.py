"""Nobody reaches into a deployment for a web-address setting any more (RC-60).

The seven settings publish-on-web owns moved from the deployment root under the service.
The relocation is only safe while EVERY reader goes through
``catalog/publish_on_web/domain_config.py``: the moment the migration runs, a reader still
doing ``deployment.get("base-domain")`` quietly gets ``None`` -- not an error, not a
warning, just a deployment that publishes on the wrong address. That is the failure this
guard exists to prevent, and it is the same shape as the ``dp-bn7`` outage: silent, and
first visible at deploy time.

Written as a source scan for the same reason ``test_service_package_is_self_contained.py``
is: the property is "no site anywhere does X", which no amount of behaviour tests can
establish, and a new site added a year from now must fail here rather than in production.
"""

from __future__ import annotations

import pathlib
import re

import opi
import pytest
from opi.services.catalog.publish_on_web.domain_config import DOMAIN_SETTING_KEYS

_OPI_ROOT = pathlib.Path(opi.__file__).parent
#: The service's own package: it IS the authority on the location, so it reads both places.
_OWNER = _OPI_ROOT / "services" / "catalog" / "publish_on_web"

#: Files allowed to address a setting at the deployment root, with the reason.
#:
#: The pre-v2.4 migration steps are the whole list. They rewrite files that predate the
#: service-config layout entirely; routing them through the accessors would have them write
#: the NEW shape into a file still stamped with an OLD version, which the per-version schema
#: then rejects. Each step relocates its own era's shape, and v2.7 relocates this one.
_ALLOWED: dict[str, str] = {
    "services/schema_migration.py": "pre-v2.4 steps rewrite the shape of their own era, before the service path existed",
}

#: ``deployment.get("base-domain")``, ``dep["subdomain"]``, ``yaml_dep.get('issuer', ...)``.
#: Matched on the KEY, so a rename of the variable holding the deployment cannot slip past.
_KEY_ALTERNATION = "|".join(re.escape(key) for key in DOMAIN_SETTING_KEYS)
_DIRECT_ACCESS = re.compile(rf"""(?:\.get\(\s*|\[\s*)(['"])({_KEY_ALTERNATION})\1""")

#: Keys that are also ordinary words elsewhere: ``issuer`` is an OIDC metadata field and a
#: cluster-config key, ``subdomain`` is a column in the global subdomain registry. Those are
#: not deployment settings, so a file that only ever uses them that way is not a violation.
#: The scan therefore only reports a hit when the expression it sits on looks like a
#: deployment: the variable is named after one.
_DEPLOYMENT_NAMES = ("deployment", "dep", "yaml_dep", "source_dep", "new_deployment", "target_deployment")


def _scanned_files() -> list[pathlib.Path]:
    return sorted(
        path
        for path in _OPI_ROOT.rglob("*.py")
        if not path.is_relative_to(_OWNER) and str(path.relative_to(_OPI_ROOT)) not in _ALLOWED
    )


def _violations(path: pathlib.Path) -> list[str]:
    found: list[str] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        for match in _DIRECT_ACCESS.finditer(line):
            prefix = line[: match.start()]
            holder = re.search(r"([A-Za-z_][A-Za-z_0-9]*)\s*$", prefix)
            if holder and holder.group(1) in _DEPLOYMENT_NAMES:
                found.append(f"{path.relative_to(_OPI_ROOT)}:{number}: {line.strip()}")
    return found


def test_the_scan_actually_finds_something() -> None:
    """Guard the guard: a regex that matches nothing would pass every test below."""
    sample = 'x = deployment.get("base-domain")'
    assert _DIRECT_ACCESS.search(sample)
    assert _DEPLOYMENT_NAMES  # and the holder check has names to match


@pytest.mark.parametrize("path", _scanned_files(), ids=lambda p: str(p.relative_to(_OPI_ROOT)))
def test_no_module_reads_a_web_address_setting_off_the_deployment(path: pathlib.Path) -> None:
    violations = _violations(path)
    assert not violations, (
        "Web-address settings live under the publish-on-web service since RC-60. Read them with "
        "opi.services.catalog.publish_on_web.domain_config.get_domain_setting (and write them with "
        "set_domain_setting), which accepts both the service path and the legacy deployment root:\n"
        + "\n".join(violations)
    )


def test_every_allowlisted_file_still_exists() -> None:
    """An allowlist entry for a file that is gone hides a rule nobody is being held to."""
    for name in _ALLOWED:
        assert (_OPI_ROOT / name).exists(), f"Allowlisted {name} no longer exists; drop the entry"
