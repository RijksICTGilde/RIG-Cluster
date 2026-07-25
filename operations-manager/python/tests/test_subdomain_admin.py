"""Tests for the subdomain/domain admin approval functionality."""

import copy

from opi.forms.visualizers.wizard_sections import _apply_approval_to_project
from opi.services.approvals import collect_approval_items

# ---------------------------------------------------------------------------
# Test data
# ---------------------------------------------------------------------------

PROJECT_DATA_WITH_DOMAINS = {
    "name": "test-project",
    "domains": {
        "allowed-domains": [
            {
                "domain": "example.nl",
                "status": "requested",
                "supports-dots": False,
                "history": [
                    {"date": "2026-04-01T10:00:00+00:00", "status": "requested"},
                ],
            },
            {
                "domain": "other.nl",
                "status": "approved",
                "supports-dots": False,
                "history": [
                    {"date": "2026-03-15T10:00:00+00:00", "status": "requested"},
                    {"date": "2026-03-16T10:00:00+00:00", "status": "approved", "by": "admin@test.nl"},
                ],
            },
        ],
        "allowed-subdomains": [
            {
                "domain": "sandbox.rijksapp.dev",
                "subdomains": [
                    {
                        "name": "myapp",
                        "status": "requested",
                        "history": [
                            {"date": "2026-04-02T10:00:00+00:00", "status": "requested"},
                        ],
                    },
                    {
                        "name": "otherapp",
                        "status": "denied",
                        "history": [
                            {"date": "2026-04-01T10:00:00+00:00", "status": "requested"},
                            {"date": "2026-04-01T12:00:00+00:00", "status": "denied", "by": "admin@test.nl"},
                        ],
                    },
                ],
            },
        ],
    },
}

PROJECT_DATA_NO_DOMAINS = {"name": "empty-project"}


# ---------------------------------------------------------------------------
# _collect_approval_items
# ---------------------------------------------------------------------------


class TestCollectApprovalItems:
    def test_collects_domains_and_subdomains(self):
        items = collect_approval_items(PROJECT_DATA_WITH_DOMAINS)
        assert len(items) == 4

        # Two allowed-domains
        domain_items = [i for i in items if i["type"] == "domain"]
        assert len(domain_items) == 2
        assert domain_items[0]["domain"] == "example.nl"
        assert domain_items[0]["current_status"] == "requested"
        assert domain_items[1]["domain"] == "other.nl"
        assert domain_items[1]["current_status"] == "approved"

        # Two subdomains
        sub_items = [i for i in items if i["type"] == "subdomain"]
        assert len(sub_items) == 2
        assert sub_items[0]["name"] == "myapp"
        assert sub_items[0]["domain"] == "sandbox.rijksapp.dev"
        assert sub_items[0]["current_status"] == "requested"
        assert sub_items[1]["name"] == "otherapp"
        assert sub_items[1]["current_status"] == "denied"

    def test_returns_empty_for_no_domains(self):
        assert collect_approval_items(PROJECT_DATA_NO_DOMAINS) == []

    def test_returns_empty_for_empty_dict(self):
        assert collect_approval_items({}) == []

    def test_all_items_default_to_skip(self):
        items = collect_approval_items(PROJECT_DATA_WITH_DOMAINS)
        for item in items:
            assert item["status"] == "skip"

    def test_history_is_included(self):
        items = collect_approval_items(PROJECT_DATA_WITH_DOMAINS)
        domain_item = next(i for i in items if i["domain"] == "other.nl")
        assert len(domain_item["history"]) == 2

    def test_handles_malformed_entries(self):
        data = {"domains": {"allowed-subdomains": ["not-a-dict", {"domain": "x.com"}]}}
        items = collect_approval_items(data)
        assert items == []


# ---------------------------------------------------------------------------
# _apply_approval_to_project (post_merge callback)
# ---------------------------------------------------------------------------


class TestApplyApprovalToProject:
    def test_approve_subdomain(self):
        project_data = copy.deepcopy(PROJECT_DATA_WITH_DOMAINS)
        wizard_data = {
            "_admin_email": "admin@test.nl",
            "_approval_items": [
                {
                    "type": "subdomain",
                    "domain": "sandbox.rijksapp.dev",
                    "name": "myapp",
                    "current_status": "requested",
                    "status": "approved",
                    "message": "Looks good",
                },
            ],
        }

        _apply_approval_to_project(project_data, wizard_data)

        subdomain = project_data["domains"]["allowed-subdomains"][0]["subdomains"][0]
        assert subdomain["status"] == "approved"
        assert len(subdomain["history"]) == 2
        last_entry = subdomain["history"][-1]
        assert last_entry["status"] == "approved"
        assert last_entry["by"] == "admin@test.nl"
        assert last_entry["message"] == "Looks good"

    def test_deny_domain(self):
        project_data = copy.deepcopy(PROJECT_DATA_WITH_DOMAINS)
        wizard_data = {
            "_admin_email": "admin@test.nl",
            "_approval_items": [
                {
                    "type": "domain",
                    "domain": "example.nl",
                    "name": "example.nl",
                    "current_status": "requested",
                    "status": "denied",
                    "message": "Not approved for this project",
                },
            ],
        }

        _apply_approval_to_project(project_data, wizard_data)

        domain = project_data["domains"]["allowed-domains"][0]
        assert domain["status"] == "denied"
        assert len(domain["history"]) == 2
        last_entry = domain["history"][-1]
        assert last_entry["status"] == "denied"
        assert last_entry["message"] == "Not approved for this project"

    def test_skip_leaves_unchanged(self):
        project_data = copy.deepcopy(PROJECT_DATA_WITH_DOMAINS)
        original_data = copy.deepcopy(PROJECT_DATA_WITH_DOMAINS)
        wizard_data = {
            "_admin_email": "admin@test.nl",
            "_approval_items": [
                {
                    "type": "subdomain",
                    "domain": "sandbox.rijksapp.dev",
                    "name": "myapp",
                    "current_status": "requested",
                    "status": "skip",
                },
            ],
        }

        _apply_approval_to_project(project_data, wizard_data)

        # Subdomain should be unchanged
        subdomain = project_data["domains"]["allowed-subdomains"][0]["subdomains"][0]
        original_sub = original_data["domains"]["allowed-subdomains"][0]["subdomains"][0]
        assert subdomain["status"] == original_sub["status"]
        assert len(subdomain["history"]) == len(original_sub["history"])

    def test_mixed_actions(self):
        project_data = copy.deepcopy(PROJECT_DATA_WITH_DOMAINS)
        wizard_data = {
            "_admin_email": "admin@test.nl",
            "_approval_items": [
                {
                    "type": "domain",
                    "domain": "example.nl",
                    "name": "example.nl",
                    "current_status": "requested",
                    "status": "approved",
                },
                {
                    "type": "subdomain",
                    "domain": "sandbox.rijksapp.dev",
                    "name": "myapp",
                    "current_status": "requested",
                    "status": "skip",
                },
                {
                    "type": "subdomain",
                    "domain": "sandbox.rijksapp.dev",
                    "name": "otherapp",
                    "current_status": "denied",
                    "status": "approved",
                },
            ],
        }

        _apply_approval_to_project(project_data, wizard_data)

        # Domain approved
        assert project_data["domains"]["allowed-domains"][0]["status"] == "approved"

        # First subdomain skipped (unchanged)
        sub0 = project_data["domains"]["allowed-subdomains"][0]["subdomains"][0]
        assert sub0["status"] == "requested"
        assert len(sub0["history"]) == 1

        # Second subdomain approved (was denied)
        sub1 = project_data["domains"]["allowed-subdomains"][0]["subdomains"][1]
        assert sub1["status"] == "approved"
        assert len(sub1["history"]) == 3

    def test_no_message_omitted_from_history(self):
        project_data = copy.deepcopy(PROJECT_DATA_WITH_DOMAINS)
        wizard_data = {
            "_admin_email": "admin@test.nl",
            "_approval_items": [
                {
                    "type": "subdomain",
                    "domain": "sandbox.rijksapp.dev",
                    "name": "myapp",
                    "current_status": "requested",
                    "status": "approved",
                },
            ],
        }

        _apply_approval_to_project(project_data, wizard_data)

        last_entry = project_data["domains"]["allowed-subdomains"][0]["subdomains"][0]["history"][-1]
        assert "message" not in last_entry

    def test_empty_items_is_noop(self):
        project_data = copy.deepcopy(PROJECT_DATA_WITH_DOMAINS)
        original = copy.deepcopy(project_data)
        wizard_data = {"_admin_email": "admin@test.nl", "_approval_items": []}

        _apply_approval_to_project(project_data, wizard_data)

        assert project_data == original

    def test_creates_domains_key_if_missing(self):
        project_data: dict = {"name": "new"}
        wizard_data = {
            "_admin_email": "admin@test.nl",
            "_approval_items": [
                {
                    "type": "subdomain",
                    "domain": "sandbox.rijksapp.dev",
                    "name": "test",
                    "current_status": "requested",
                    "status": "approved",
                },
            ],
        }

        # Should not raise, even though no matching entry exists
        _apply_approval_to_project(project_data, wizard_data)
        assert "domains" in project_data
