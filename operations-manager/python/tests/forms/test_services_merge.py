"""Services list is merged by service name, not by index.

Regression: the modal-edit-services wizard duplicated a service (and dropped a
just-added one) because two sections each carried a full ``services`` list and the
merge combined them BY INDEX. The attachments carrier still held the pre-edit list,
so index 1 (a newly ticked service) got swapped for attachments and attachments ended
up twice. Merging by name fixes it.
"""

from __future__ import annotations

from opi.forms.wizard.services_merge import merge_service_lists, service_name
from opi.forms.wizard.state import WizardState

ATTACH = {"attachments": {"data": [{"id": "test", "filename": "1.rss", "content": "AGE..."}]}}


def _names(services: list) -> list[str | None]:
    return [service_name(e) for e in services]


def test_merge_no_duplicate_and_keeps_new_service() -> None:
    # picker adds temp-storage; attachments carrier still holds the pre-edit list
    result = merge_service_lists(
        ["publish-on-web", "temp-storage", ATTACH],
        ["publish-on-web", ATTACH],
    )
    assert _names(result) == ["publish-on-web", "temp-storage", "attachments"]
    assert result.count(ATTACH) <= 1  # attachments not duplicated


def test_merge_preserves_attachments_data() -> None:
    result = merge_service_lists(["publish-on-web", ATTACH], ["publish-on-web", "attachments"])
    attach_entry = next(e for e in result if service_name(e) == "attachments")
    assert attach_entry["attachments"]["data"][0]["id"] == "test"


def test_merge_promotes_bare_string_to_config_dict() -> None:
    kc = {"keycloak": {"config": {"realm": "x"}}}
    result = merge_service_lists(["publish-on-web", "keycloak"], ["publish-on-web", kc])
    kc_entry = next(e for e in result if service_name(e) == "keycloak")
    assert kc_entry == kc


def test_get_merged_data_adds_service_without_duplicating_attachments() -> None:
    """The exact modal-edit-services scenario: tick temp-storage -> it is added,
    attachments stays once with its data, nothing swapped."""
    state = WizardState(
        flow_id="modal-edit-services",
        current_step="attachments",
        step_data={
            "services-edit": {"services": ["publish-on-web", "temp-storage", ATTACH]},
            "attachments": {"services": ["publish-on-web", ATTACH]},
        },
        active_sections=["services-edit", "attachments"],
    )
    services = state.get_merged_data()["services"]
    assert _names(services) == ["publish-on-web", "temp-storage", "attachments"]
    attach_entry = next(e for e in services if service_name(e) == "attachments")
    assert attach_entry["attachments"]["data"][0]["id"] == "test"


def test_get_merged_data_deselect_drops_service() -> None:
    state = WizardState(
        flow_id="modal-edit-services",
        current_step="services-edit",
        step_data={"services-edit": {"services": ["publish-on-web"]}},
        active_sections=["services-edit"],
    )
    assert _names(state.get_merged_data()["services"]) == ["publish-on-web"]
