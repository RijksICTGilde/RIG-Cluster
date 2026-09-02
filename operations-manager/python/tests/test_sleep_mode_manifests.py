"""Tests for sleep-mode waker component selection and manifest values."""

import pytest
from opi.generation.manifests import render_template
from opi.handlers.project_file_handler import ProjectFileHandler
from opi.services.catalog.sleep_mode import manifests
from opi.services.catalog.sleep_mode.config_model import SleepModeConfig
from ruamel.yaml import YAML


@pytest.fixture
def handler() -> ProjectFileHandler:
    return ProjectFileHandler()


def _project(components: list[dict], deployment_components: list[dict]) -> dict:
    return {
        "name": "proj",
        "components": components,
        "deployments": [{"name": "PR-1", "components": deployment_components}],
    }


class TestSelectWakerComponent:
    def test_single_web_component_selected(self, handler: ProjectFileHandler) -> None:
        project = _project(
            [{"name": "frontend", "services": ["publish-on-web"]}, {"name": "worker", "services": []}],
            [{"reference": "frontend"}, {"reference": "worker"}],
        )
        config = SleepModeConfig(enabled=True)
        assert manifests.select_waker_component(project, project["deployments"][0], config, handler) == "frontend"

    def test_no_web_component_no_waker(self, handler: ProjectFileHandler) -> None:
        project = _project([{"name": "worker", "services": []}], [{"reference": "worker"}])
        config = SleepModeConfig(enabled=True)
        assert manifests.select_waker_component(project, project["deployments"][0], config, handler) is None

    def test_two_web_components_without_waker_component_no_waker(self, handler: ProjectFileHandler) -> None:
        project = _project(
            [
                {"name": "frontend", "services": ["publish-on-web"]},
                {"name": "admin", "services": ["publish-on-web"]},
            ],
            [{"reference": "frontend"}, {"reference": "admin"}],
        )
        config = SleepModeConfig(enabled=True)
        assert manifests.select_waker_component(project, project["deployments"][0], config, handler) is None

    def test_two_web_components_with_waker_component(self, handler: ProjectFileHandler) -> None:
        project = _project(
            [
                {"name": "frontend", "services": ["publish-on-web"]},
                {"name": "admin", "services": ["publish-on-web"]},
            ],
            [{"reference": "frontend"}, {"reference": "admin"}],
        )
        config = SleepModeConfig(enabled=True, waker_component="admin")
        assert manifests.select_waker_component(project, project["deployments"][0], config, handler) == "admin"

    def test_waker_component_not_web_no_waker(self, handler: ProjectFileHandler) -> None:
        project = _project(
            [{"name": "frontend", "services": ["publish-on-web"]}, {"name": "worker", "services": []}],
            [{"reference": "frontend"}, {"reference": "worker"}],
        )
        config = SleepModeConfig(enabled=True, waker_component="worker")
        assert manifests.select_waker_component(project, project["deployments"][0], config, handler) is None

    def test_a_component_behind_the_authorization_wall_is_a_candidate(self, handler: ProjectFileHandler) -> None:
        """A walled component gets a waker like any other.

        The wall moves the Service to the oauth2-proxy port and the caller hands that
        port to the waker, so the waker answers there without the proxy: while the
        deployment sleeps its hostname serves the wake page anonymously. Accepted, so
        that a walled deployment can be woken from its own hostname.
        """
        project = _project(
            [{"name": "frontend", "services": ["publish-on-web", "authorization-wall"]}],
            [{"reference": "frontend"}],
        )
        config = SleepModeConfig(enabled=True)
        assert manifests.select_waker_component(project, project["deployments"][0], config, handler) == "frontend"

    def test_naming_a_walled_component_explicitly_picks_it(self, handler: ProjectFileHandler) -> None:
        """The wall is not a reason to refuse the component somebody named."""
        project = _project(
            [
                {"name": "frontend", "services": ["publish-on-web"]},
                {"name": "admin", "services": ["publish-on-web", "authorization-wall"]},
            ],
            [{"reference": "frontend"}, {"reference": "admin"}],
        )
        config = SleepModeConfig(enabled=True, waker_component="admin")
        assert manifests.select_waker_component(project, project["deployments"][0], config, handler) == "admin"

    def test_a_walled_component_counts_as_a_candidate_for_the_ambiguity_rule(self, handler: ProjectFileHandler) -> None:
        """Two web components, one of them walled, is still two candidates: no waker
        until ``waker-component`` says which hostname visitors reach first."""
        project = _project(
            [
                {"name": "frontend", "services": ["publish-on-web"]},
                {"name": "admin", "services": ["publish-on-web", "authorization-wall"]},
            ],
            [{"reference": "frontend"}, {"reference": "admin"}],
        )
        config = SleepModeConfig(enabled=True)
        assert manifests.select_waker_component(project, project["deployments"][0], config, handler) is None

    def test_passthrough_tls_excluded(self, handler: ProjectFileHandler) -> None:
        project = _project(
            [
                {
                    "name": "frontend",
                    "services": [{"publish-on-web": {"config": {"tls": "passthrough"}}}],
                }
            ],
            [{"reference": "frontend"}],
        )
        config = SleepModeConfig(enabled=True)
        assert manifests.select_waker_component(project, project["deployments"][0], config, handler) is None


class TestOpsApiUrl:
    def test_local(self) -> None:
        assert manifests.ops_api_url("local") == "http://operations-manager.rig-system.svc.cluster.local:8000"

    def test_production(self) -> None:
        assert (
            manifests.ops_api_url("odcn-production")
            == "http://operations-manager.rig-prd-operations.svc.cluster.local:8000"
        )


class TestBuildWakerDeploymentValues:
    def test_names_and_selectors(self) -> None:
        values = manifests.build_waker_deployment_values(
            app_name="PR-1-frontend",
            namespace="rig-proj",
            project_name="proj",
            deployment_name="PR-1",
            cluster="local",
            port=8080,
        )
        assert values["object_name"] == "PR-1-frontend-waker"
        assert values["name"] == "PR-1-frontend"
        assert values["extra_selector_labels"] == {"zad-role": "waker"}
        assert values["env_from_configmaps"] == ["PR-1-frontend-waker-config"]
        assert values["env_from_secrets"] == ["PR-1-frontend-waker-token"]
        assert values["probe_readiness_failure_threshold"] == 1
        # :latest default -> Always
        assert values["imagePullPolicy"] == "Always"

    def test_pinned_image_tag_uses_ifnotpresent(self, monkeypatch) -> None:
        from opi.core.config import settings

        monkeypatch.setattr(settings, "SLEEP_MODE_WAKER_IMAGE", "zad-waker:test")
        values = manifests.build_waker_deployment_values(
            app_name="a", namespace="ns", project_name="p", deployment_name="PR-1", cluster="local", port=8080
        )
        assert values["imagePullPolicy"] == "IfNotPresent"

    def test_renders_valid_deployment(self) -> None:
        values = manifests.build_waker_deployment_values(
            app_name="PR-1-frontend",
            namespace="rig-proj",
            project_name="proj",
            deployment_name="PR-1",
            cluster="local",
            port=8080,
            generated_at="2026-01-01T00:00:00Z",
        )
        doc = YAML().load(render_template("deployment.yaml.jinja", values))
        assert doc["metadata"]["name"] == "PR-1-frontend-waker"
        assert doc["metadata"]["labels"]["app"] == "PR-1-frontend"
        assert doc["spec"]["selector"]["matchLabels"]["zad-role"] == "waker"
        assert doc["spec"]["template"]["metadata"]["labels"]["zad-role"] == "waker"
        env_from = doc["spec"]["template"]["spec"]["containers"][0]["envFrom"]
        assert {"configMapRef": {"name": "PR-1-frontend-waker-config"}} in env_from
        assert {"secretRef": {"name": "PR-1-frontend-waker-token"}} in env_from
        assert doc["spec"]["template"]["spec"]["containers"][0]["readinessProbe"]["failureThreshold"] == 1


class TestBuildWakerConfigmapValues:
    def test_data_and_default_title(self) -> None:
        config = SleepModeConfig(enabled=True, wake_mode="confirm", description="Preview")
        values = manifests.build_waker_configmap_values(
            app_name="PR-1-frontend",
            namespace="rig-proj",
            project_name="proj",
            deployment_name="PR-1",
            component_reference="frontend",
            config=config,
            cluster="local",
            port=8080,
        )
        data = values["data"]
        assert values["name"] == "PR-1-frontend-waker-config"
        assert values["app_label"] == "PR-1-frontend"
        assert data["ZAD_APP_TITLE"] == "PR-1"  # default title is the deployment name
        assert data["ZAD_WAKE_MODE"] == "confirm"
        assert data["ZAD_APP_DESCRIPTION"] == "Preview"
        assert data["ZAD_API_URL"] == "http://operations-manager.rig-system.svc.cluster.local:8000"
        assert data["ZAD_POLL_INTERVAL_SEC"] == "3"

    def test_title_placeholders(self) -> None:
        config = SleepModeConfig(enabled=True, title="{project} - {deployment}")
        values = manifests.build_waker_configmap_values(
            app_name="a",
            namespace="ns",
            project_name="proj",
            deployment_name="PR-9",
            component_reference="frontend",
            config=config,
            cluster="local",
            port=8080,
        )
        assert values["data"]["ZAD_APP_TITLE"] == "proj - PR-9"

    def test_renders_valid_configmap(self) -> None:
        config = SleepModeConfig(enabled=True)
        values = manifests.build_waker_configmap_values(
            app_name="a",
            namespace="ns",
            project_name="proj",
            deployment_name="PR-1",
            component_reference="frontend",
            config=config,
            cluster="local",
            port=8080,
        )
        doc = YAML().load(render_template("configmap.yaml.jinja", values))
        assert doc["kind"] == "ConfigMap"
        assert doc["data"]["ZAD_DEPLOYMENT"] == "PR-1"


class TestTheWakerListensWhereTheServiceSends:
    """The waker has no Service of its own; it joins the application's by carrying the
    same ``app`` label. So its port is not a property of the waker, it is a property of
    the Service it sits behind, and the two drifting apart is invisible: the Service
    selects the pod, the pod passes its own probes, and the hostname answers nothing.

    That is what a hardcoded 8080 did. On production, wies serves on 8000 and had sixteen
    sleeping previews with no waiting page; asses-k2n happens to use 8080 and worked.
    """

    def test_the_container_port_follows_the_service(self) -> None:
        values = manifests.build_waker_deployment_values(
            app_name="pr-274-frontend",
            namespace="rig-prd-wies",
            project_name="wies",
            deployment_name="pr-274",
            cluster="odcn-production",
            port=8000,
        )
        assert values["inbound_ports"] == [8000]
        assert values["application_port"] == 8000

        doc = YAML().load(render_template("deployment.yaml.jinja", values))
        container = doc["spec"]["template"]["spec"]["containers"][0]
        assert container["ports"][0]["containerPort"] == 8000
        # The probes ride along, or a correct container port would still be reported
        # unhealthy on the old one.
        assert container["readinessProbe"]["httpGet"]["port"] == 8000
        assert container["livenessProbe"]["httpGet"]["port"] == 8000

    def test_the_process_is_told_the_same_port(self) -> None:
        """Declaring the container port is half of it: the image has to listen there too,
        and it reads ZAD_PORT (default 8080 when absent, so an older image is unchanged)."""
        values = manifests.build_waker_configmap_values(
            app_name="pr-274-frontend",
            namespace="rig-prd-wies",
            project_name="wies",
            deployment_name="pr-274",
            component_reference="frontend",
            config=SleepModeConfig(enabled=True),
            cluster="odcn-production",
            port=8000,
        )
        assert values["data"]["ZAD_PORT"] == "8000"

    def test_the_manifest_and_the_configmap_never_disagree(self) -> None:
        """The two halves come from one argument, so they cannot drift apart."""
        for port in (8000, 8080, 3000):
            deployment = manifests.build_waker_deployment_values(
                app_name="a",
                namespace="ns",
                project_name="p",
                deployment_name="d",
                cluster="local",
                port=port,
            )
            configmap = manifests.build_waker_configmap_values(
                app_name="a",
                namespace="ns",
                project_name="p",
                deployment_name="d",
                component_reference="c",
                config=SleepModeConfig(enabled=True),
                cluster="local",
                port=port,
            )
            assert deployment["application_port"] == int(configmap["data"]["ZAD_PORT"])
