"""Tests for _pad_sparse_submission in router_detail_edit."""

from opi.web.router_detail_edit import _pad_sparse_submission


class TestPadSparseSubmission:
    def test_component_index_0_no_padding(self):
        body = {"components": [{"name": "web"}]}
        result = _pad_sparse_submission(body, "modal-edit-component-0")
        assert result["components"] == [{"name": "web"}]

    def test_component_index_1_pads(self):
        body = {"components": [{"name": "api"}]}
        result = _pad_sparse_submission(body, "modal-edit-component-1")
        assert len(result["components"]) == 2
        assert result["components"][0] == {}
        assert result["components"][1] == {"name": "api"}

    def test_component_index_2_pads(self):
        body = {"components": [{"name": "worker"}]}
        result = _pad_sparse_submission(body, "modal-edit-component-2")
        assert len(result["components"]) == 3
        assert result["components"][0] == {}
        assert result["components"][1] == {}
        assert result["components"][2] == {"name": "worker"}

    def test_deployment_index_1_pads(self):
        body = {"deployments": [{"name": "staging"}]}
        result = _pad_sparse_submission(body, "modal-edit-deployment-1")
        assert len(result["deployments"]) == 2
        assert result["deployments"][1] == {"name": "staging"}

    def test_domain_index_1_pads_deployments(self):
        body = {"deployments": [{"domain": "example.nl"}]}
        result = _pad_sparse_submission(body, "modal-edit-domain-1")
        assert len(result["deployments"]) == 2
        assert result["deployments"][1] == {"domain": "example.nl"}

    def test_unrelated_flow_no_change(self):
        body = {"services": ["keycloak"]}
        result = _pad_sparse_submission(body, "modal-edit-services")
        assert result is body

    def test_empty_array_no_padding(self):
        body = {"components": []}
        result = _pad_sparse_submission(body, "modal-edit-component-1")
        # Empty array means no data submitted, don't pad
        assert result is body

    def test_missing_key_no_padding(self):
        body = {"other": "data"}
        result = _pad_sparse_submission(body, "modal-edit-component-1")
        assert result is body

    def test_does_not_mutate_original(self):
        body = {"components": [{"name": "api"}]}
        result = _pad_sparse_submission(body, "modal-edit-component-1")
        assert len(body["components"]) == 1  # original unchanged
        assert len(result["components"]) == 2
