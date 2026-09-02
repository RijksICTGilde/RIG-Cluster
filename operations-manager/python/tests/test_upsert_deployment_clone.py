"""Tests for the config-clone behavior of upsert_deployment (create path).

Cloning a deployment (cloneFrom, used by CI to create PR previews) deep-copies
the source deployment's config. Several fields must NOT be copied:

- The backup block: inheriting the source's schedule made every PR preview
  accumulate nightly snapshots that nothing ever cleaned up.
- The custom-domain config (base-domain, domain-mode, domain-format, issuer):
  cloned deployments use the default cluster domain. domain-format in
  particular is dangerous to inherit: a dot-based format like
  ``component.subdomain`` without the source's base-domain resolves onto the
  cluster wildcard, producing a multi-label host the single-label wildcard
  cert cannot cover (browser cert warning).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from opi.services.catalog.publish_on_web.domain_config import DomainSetting, get_domain_setting
from opi.services.services import service_entry_name


def _make_manager():
    with (
        patch("opi.manager.project_manager.KubectlConnector"),
        patch("opi.handlers.sops.SopsHandler"),
        patch("opi.generation.manifests.ManifestGenerator"),
        patch("opi.manager.argo_manager.ArgoManager", return_value=MagicMock()),
        patch("opi.manager.bootstrap_manager.BootstrapManager", return_value=MagicMock()),
        patch("opi.manager.delete_project_manager.DeleteProjectManager", return_value=MagicMock()),
        patch("opi.manager.keycloak_manager.KeycloakManager", return_value=MagicMock()),
        patch("opi.manager.minio_manager.MinioManager", return_value=MagicMock()),
        patch("opi.manager.redis_manager.RedisManager", return_value=MagicMock()),
        patch("opi.manager.pvc_manager.PVCManager", return_value=MagicMock()),
    ):
        from opi.manager.project_manager import ProjectManager

        pm = ProjectManager()
        return pm


def _wire_create_mocks(pm, project_data: dict) -> AsyncMock:
    """Mock the collaborators of the create path; returns the save_and_commit mock."""
    pm.get_contents = AsyncMock(return_value=project_data)
    pm.get_name = AsyncMock(return_value="demo")
    pm.get_deployments = AsyncMock(return_value=project_data["deployments"])
    pm._validate_component_references = MagicMock(return_value={"success": True, "error": None})
    save = AsyncMock()
    pm.save_and_commit_project = save
    return save


def _project_with_scheduled_source() -> dict:
    return {
        "name": "demo",
        "clusters": ["odcn-production"],
        "repositories": [{"name": "main-repo"}],
        "deployments": [
            {
                "name": "production",
                "cluster": "odcn-production",
                "namespace": "demo",
                "domain-format": "subdomain-only",
                "backup": {
                    "resource_types": ["database"],
                    "schedule": "FREQ=DAILY;BYHOUR=2;BYMINUTE=0",
                },
                "components": [{"reference": "frontend", "image": "ghcr.io/org/app:v1"}],
            }
        ],
    }


class TestUpsertDeploymentClone:
    async def test_clone_does_not_inherit_backup_block(self):
        pm = _make_manager()
        project_data = _project_with_scheduled_source()
        _wire_create_mocks(pm, project_data)

        with patch("opi.manager.project_manager.ensure_domain_requests"):
            result = await pm.upsert_deployment(
                deployment_name="pr-123",
                components=[SimpleNamespace(reference="frontend", image="ghcr.io/org/app:pr-123")],
                clone_from="production",
            )

        assert result["success"] is True
        assert result["created"] is True

        new_deployment = next(d for d in project_data["deployments"] if d["name"] == "pr-123")
        assert "backup" not in new_deployment

    async def test_clone_still_inherits_other_config(self):
        pm = _make_manager()
        project_data = _project_with_scheduled_source()
        _wire_create_mocks(pm, project_data)

        with patch("opi.manager.project_manager.ensure_domain_requests"):
            result = await pm.upsert_deployment(
                deployment_name="pr-123",
                components=[SimpleNamespace(reference="frontend", image="ghcr.io/org/app:pr-123")],
                clone_from="production",
            )

        assert result["success"] is True
        new_deployment = next(d for d in project_data["deployments"] if d["name"] == "pr-123")
        assert new_deployment["cluster"] == "odcn-production"
        assert new_deployment["clone-from"] == {"type": "deployment", "reference": "production", "mode": "once"}

    async def test_clone_does_not_inherit_dot_domain_format(self):
        """Regression: a clone of a nice-url/dot deployment must not inherit
        its domain-format, base-domain, domain-mode or subdomain.

        Mirrors the real regel-k4c bug: PR previews cloned from the production
        ``regelrecht`` deployment (domain-mode nice-url, base-domain rijks.app,
        domain-format component.subdomain) inherited the dot format but lost
        base-domain, so the host resolved to ``editor.<pr>.<cluster-wildcard>``
        which the single-label wildcard cert does not cover.
        """
        pm = _make_manager()
        project_data = {
            "name": "demo",
            "clusters": ["odcn-production"],
            "repositories": [{"name": "main-repo"}],
            "deployments": [
                {
                    "name": "regelrecht",
                    "cluster": "odcn-production",
                    "namespace": "demo",
                    "domain-mode": "nice-url",
                    "subdomain": "regelrecht",
                    "base-domain": "rijks.app",
                    "domain-format": "component.subdomain",
                    "issuer": "letsencrypt",
                    "components": [{"reference": "editor", "image": "ghcr.io/org/editor:v1"}],
                }
            ],
        }
        _wire_create_mocks(pm, project_data)

        with patch("opi.manager.project_manager.ensure_domain_requests"):
            result = await pm.upsert_deployment(
                deployment_name="pr857",
                components=[SimpleNamespace(reference="editor", image="ghcr.io/org/editor:pr857")],
                clone_from="regelrecht",
            )

        assert result["success"] is True
        new_deployment = next(d for d in project_data["deployments"] if d["name"] == "pr857")
        # The custom-domain config must NOT leak into the clone.
        assert "domain-format" not in new_deployment
        assert "base-domain" not in new_deployment
        assert "domain-mode" not in new_deployment
        assert "issuer" not in new_deployment

    async def test_clone_drops_root_component_when_target_has_no_root_format(self):
        """A clone uses the default cluster domain (no nice-url / dot format), so an
        inherited root-component would be inert. It must be dropped (mirrors pr884)."""
        pm = _make_manager()
        project_data = {
            "name": "demo",
            "clusters": ["odcn-production"],
            "repositories": [{"name": "main-repo"}],
            "deployments": [
                {
                    "name": "regelrecht",
                    "cluster": "odcn-production",
                    "namespace": "demo",
                    "domain-mode": "nice-url",
                    "subdomain": "regelrecht",
                    "base-domain": "rijks.app",
                    "domain-format": "component.subdomain",
                    "root-component": "editor",
                    "components": [{"reference": "editor", "image": "ghcr.io/org/editor:v1"}],
                }
            ],
        }
        _wire_create_mocks(pm, project_data)

        with patch("opi.manager.project_manager.ensure_domain_requests"):
            result = await pm.upsert_deployment(
                deployment_name="pr884",
                components=[SimpleNamespace(reference="editor", image="ghcr.io/org/editor:pr884")],
                clone_from="regelrecht",
            )

        assert result["success"] is True
        new_deployment = next(d for d in project_data["deployments"] if d["name"] == "pr884")
        assert "root-component" not in new_deployment

    async def test_clone_does_not_inherit_web_address_stored_under_the_service(self):
        """Regression (RC-60): the web address moved into the source's ``services`` block.

        Excluding the seven ROOT key names from the copy became a silent no-op the moment
        the values moved, because ``services`` is copied as a whole. A clone would then
        generate ingresses on EXACTLY the source's hostnames.
        """
        pm = _make_manager()
        project_data = {
            "name": "demo",
            "clusters": ["odcn-production"],
            "repositories": [{"name": "main-repo"}],
            "deployments": [
                {
                    "name": "productie",
                    "cluster": "odcn-production",
                    "namespace": "demo",
                    "services": [
                        {
                            "reference": "publish-on-web",
                            "config": {
                                "subdomain": "wies",
                                "base-domain": "rijksapps.nl",
                                "domain-format": "component.subdomain",
                                "domain-mode": "nice-url",
                                "issuer": "letsencrypt",
                                "root-component": "editor",
                                "expose-component-on-bare-domain": "editor",
                            },
                        }
                    ],
                    "components": [{"reference": "editor", "image": "ghcr.io/org/editor:v1"}],
                }
            ],
        }
        _wire_create_mocks(pm, project_data)

        with patch("opi.manager.project_manager.ensure_domain_requests"):
            result = await pm.upsert_deployment(
                deployment_name="pr857",
                components=[SimpleNamespace(reference="editor", image="ghcr.io/org/editor:pr857")],
                clone_from="productie",
            )

        assert result["success"] is True
        new_deployment = next(d for d in project_data["deployments"] if d["name"] == "pr857")
        for setting in DomainSetting:
            assert get_domain_setting(new_deployment, setting) is None, f"{setting.value} was inherited"
        # And no empty publish-on-web record left behind for a clone that has no web address.
        assert new_deployment.get("services") is None

    async def test_clone_keeps_the_subdomain_the_caller_asked_for(self):
        """The requested settings are written BEFORE the copy from the source lands on the
        same service block. They must survive it -- otherwise the clone silently claims the
        source's hostname instead of its own."""
        pm = _make_manager()
        project_data = {
            "name": "demo",
            "clusters": ["odcn-production"],
            "repositories": [{"name": "main-repo"}],
            "deployments": [
                {
                    "name": "productie",
                    "cluster": "odcn-production",
                    "namespace": "demo",
                    "services": [
                        {
                            "reference": "publish-on-web",
                            "config": {"subdomain": "wies", "base-domain": "rijksapps.nl"},
                        }
                    ],
                    "components": [{"reference": "editor", "image": "ghcr.io/org/editor:v1"}],
                }
            ],
        }
        _wire_create_mocks(pm, project_data)
        pm._enforce_domain_config = AsyncMock(return_value=None)

        with patch("opi.manager.project_manager.ensure_domain_requests"):
            result = await pm.upsert_deployment(
                deployment_name="pr-123",
                components=[SimpleNamespace(reference="editor", image="ghcr.io/org/editor:pr-123")],
                clone_from="productie",
                subdomain="pr-123",
            )

        assert result["success"] is True
        new_deployment = next(d for d in project_data["deployments"] if d["name"] == "pr-123")
        assert get_domain_setting(new_deployment, DomainSetting.SUBDOMAIN) == "pr-123"
        assert get_domain_setting(new_deployment, DomainSetting.BASE_DOMAIN) is None

    async def test_clone_keeps_other_service_entries(self):
        """Dropping the web address must not drop the source's other deployment services."""
        pm = _make_manager()
        project_data = {
            "name": "demo",
            "clusters": ["odcn-production"],
            "repositories": [{"name": "main-repo"}],
            "deployments": [
                {
                    "name": "productie",
                    "cluster": "odcn-production",
                    "namespace": "demo",
                    "services": [
                        {"reference": "publish-on-web", "config": {"subdomain": "wies"}},
                        {"reference": "cross-domain-access", "config": {"allowed-origins": ["https://x.nl"]}},
                    ],
                    "components": [{"reference": "editor", "image": "ghcr.io/org/editor:v1"}],
                }
            ],
        }
        _wire_create_mocks(pm, project_data)

        with patch("opi.manager.project_manager.ensure_domain_requests"):
            await pm.upsert_deployment(
                deployment_name="pr857",
                components=[SimpleNamespace(reference="editor", image="ghcr.io/org/editor:pr857")],
                clone_from="productie",
            )

        new_deployment = next(d for d in project_data["deployments"] if d["name"] == "pr857")
        assert [service_entry_name(entry) for entry in new_deployment["services"]] == ["cross-domain-access"]

    async def test_clone_leaves_source_backup_untouched(self):
        pm = _make_manager()
        project_data = _project_with_scheduled_source()
        _wire_create_mocks(pm, project_data)

        with patch("opi.manager.project_manager.ensure_domain_requests"):
            await pm.upsert_deployment(
                deployment_name="pr-123",
                components=[SimpleNamespace(reference="frontend", image="ghcr.io/org/app:pr-123")],
                clone_from="production",
            )

        source = next(d for d in project_data["deployments"] if d["name"] == "production")
        assert source["backup"]["schedule"] == "FREQ=DAILY;BYHOUR=2;BYMINUTE=0"

    async def test_clone_does_not_inherit_sleep_state(self):
        """Regression (asses-k2n, 24 August): a preview cloned from a sleeping source
        was created asleep.

        ``deployments[].sleep`` is sleep-mode's own record about the SOURCE: which
        deployment is asleep, and that deployment's wake token. Copied along, the new
        preview rendered at ``replicas: 0`` on its very first sync -- rolled out asleep
        before anyone could look at it -- and carried the source's wake token, so one
        token woke two deployments.
        """
        pm = _make_manager()
        project_data = _project_with_scheduled_source()
        source = project_data["deployments"][0]
        source["sleep"] = {"state": "sleeping", "wake-token": "-----BEGIN AGE ENCRYPTED FILE-----"}
        _wire_create_mocks(pm, project_data)

        with patch("opi.manager.project_manager.ensure_domain_requests"):
            result = await pm.upsert_deployment(
                deployment_name="pr-123",
                components=[SimpleNamespace(reference="frontend", image="ghcr.io/org/app:pr-123")],
                clone_from="production",
            )

        assert result["success"] is True
        new_deployment = next(d for d in project_data["deployments"] if d["name"] == "pr-123")
        assert "sleep" not in new_deployment
        # And the source keeps its own state: the clone reads it, never moves it.
        assert source["sleep"]["state"] == "sleeping"

    async def test_create_runs_the_redeploy_hooks(self):
        """Creating a deployment is a rollout, so the services get that moment too.

        For sleep-mode that means the sleep clock starts in the commit that creates the
        deployment, instead of the new preview waiting for the next sweep to be given a
        deadline.
        """
        pm = _make_manager()
        project_data = _project_with_scheduled_source()
        project_data["services"] = [
            {"name": "sleep-mode", "config": {"enabled": True, "match": ["pr-*"], "sleep-after-deploy": "4h"}}
        ]
        _wire_create_mocks(pm, project_data)

        with patch("opi.manager.project_manager.ensure_domain_requests"):
            result = await pm.upsert_deployment(
                deployment_name="pr-123",
                components=[SimpleNamespace(reference="frontend", image="ghcr.io/org/app:pr-123")],
                clone_from="production",
            )

        assert result["success"] is True
        new_deployment = next(d for d in project_data["deployments"] if d["name"] == "pr-123")
        assert new_deployment["sleep"]["state"] == "awake"
        assert new_deployment["sleep"]["expires-at"]
        assert "wake-token" not in new_deployment["sleep"]
