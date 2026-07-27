"""The component services picker is filtered on the project's service names.

Those names must be read through service_entry_name, so every entry form resolves.
Reading a record's raw dict keys returned "name"/"config" and dropped the service, which
made every config-carrying service (keycloak, publish-on-web with domains, attachments)
disappear from a component's picker: a newly added component came up with only the
config-less half of the project's services ticked.
"""

from opi.forms.renderer import FormRenderer


def _context(services: list) -> dict:
    return FormRenderer._build_provider_context({"services": services})


def test_bare_strings():
    assert _context(["publish-on-web", "redis"])["project_services"] == ["publish-on-web", "redis"]


def test_uniform_records():
    services = [
        "redis",
        {"name": "keycloak", "config": {"template": "sso-only"}},
        {"name": "publish-on-web", "config": {"domains": {}}},
    ]
    assert _context(services)["project_services"] == ["redis", "keycloak", "publish-on-web"]


def test_legacy_single_key_dicts():
    services = [{"attachments": {"data": []}}, "redis"]
    assert _context(services)["project_services"] == ["attachments", "redis"]


def test_unrecognisable_entry_is_skipped():
    assert _context([{"config": {}}, "redis"])["project_services"] == ["redis"]
