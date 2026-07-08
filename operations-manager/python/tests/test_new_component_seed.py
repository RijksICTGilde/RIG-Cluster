"""New-component seed must materialise services into a list, not literal {K} keys.

Regression for the prod modal add-component bug (mpfm-w3h): the seed for a fresh
component wrote editable path-filter notation (services{metrics-scraper}, ...) as
literal component keys, which additionalProperties:false rejects, so every add of a
component with services failed with "Additional properties are not allowed".
"""

from opi.forms.visualizers.fields.components import COMPONENTS_SEQUENCE
from opi.web.router_wizard import _empty_sequence_item


def test_seed_has_no_leaked_filter_notation_keys() -> None:
    item = _empty_sequence_item(COMPONENTS_SEQUENCE)
    assert isinstance(item, dict)
    # The bug: keys like "services{metrics-scraper}" leaked onto the component.
    leaked = [k for k in item if "{" in k or "}" in k]
    assert leaked == [], f"editable filter-notation leaked as literal component keys: {leaked}"


def test_seed_services_is_a_list() -> None:
    item = _empty_sequence_item(COMPONENTS_SEQUENCE)
    # If services were seeded at all, they must be a proper list of entries.
    if "services" in item:
        assert isinstance(item["services"], list)
        for entry in item["services"]:
            assert isinstance(entry, (str, dict))
