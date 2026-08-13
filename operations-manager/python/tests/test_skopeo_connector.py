"""
Tests for the skopeo connector.

Tests singleton pattern, validation, command construction, and push execution
with mocked subprocess calls.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.connectors.skopeo import SkopeoConnectionError, SkopeoConnector, SkopeoExecutionError, SkopeoValidationError


@pytest.fixture(autouse=True)
def _reset_singleton():
    """Reset the singleton between tests."""
    SkopeoConnector._instance = None
    yield
    SkopeoConnector._instance = None


@pytest.fixture
def connector():
    """Create a connector with skopeo mocked as available."""
    with (
        patch("opi.connectors.skopeo.subprocess.run") as mock_run,
        patch("opi.connectors.skopeo.decrypt_password_smart_auto_sync", return_value="decrypted-token"),
        patch("opi.connectors.skopeo.settings") as mock_settings,
    ):
        mock_run.return_value = MagicMock(returncode=0, stdout="skopeo version 1.14.0", stderr="")
        mock_settings.REGISTRY_URL = "rcr.rijksapps.nl"
        mock_settings.REGISTRY_ORG = "rig"
        mock_settings.REGISTRY_USERNAME = "rig+zad"
        mock_settings.REGISTRY_PASSWORD = "age:encrypted-token"
        mock_settings.REGISTRY_VERIFY_TLS = True
        mock_settings.IMAGE_UPLOAD_MAX_SIZE_MB = 5120
        c = SkopeoConnector()
        # Patch settings on the module level for method calls
        with patch("opi.connectors.skopeo.settings", mock_settings):
            yield c


class TestSingleton:
    def test_returns_same_instance(self):
        with (
            patch("opi.connectors.skopeo.subprocess.run") as mock_run,
            patch("opi.connectors.skopeo.settings") as mock_settings,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="v1.14.0", stderr="")
            mock_settings.REGISTRY_PASSWORD = ""
            a = SkopeoConnector()
            b = SkopeoConnector()
            assert a is b


class TestAvailability:
    def test_skopeo_not_found(self):
        with (
            patch("opi.connectors.skopeo.subprocess.run", side_effect=FileNotFoundError()),
            patch("opi.connectors.skopeo.settings") as mock_settings,
        ):
            mock_settings.REGISTRY_PASSWORD = ""
            c = SkopeoConnector()
            assert c.is_skopeo_available is False

    def test_skopeo_available(self):
        with (
            patch("opi.connectors.skopeo.subprocess.run") as mock_run,
            patch("opi.connectors.skopeo.settings") as mock_settings,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="skopeo version 1.14.0", stderr="")
            mock_settings.REGISTRY_PASSWORD = ""
            c = SkopeoConnector()
            assert c.is_skopeo_available is True


class TestValidation:
    def test_valid_image_names(self, connector):
        # Should not raise
        connector._validate_image_name("myapp")
        connector._validate_image_name("my-app")
        connector._validate_image_name("my.app")
        connector._validate_image_name("my_app")
        connector._validate_image_name("app123")

    def test_invalid_image_names(self, connector):
        with pytest.raises(SkopeoValidationError):
            connector._validate_image_name("")
        with pytest.raises(SkopeoValidationError):
            connector._validate_image_name("MyApp")  # uppercase
        with pytest.raises(SkopeoValidationError):
            connector._validate_image_name("-app")  # starts with hyphen

    def test_valid_tags(self, connector):
        connector._validate_tag("v1.2.3")
        connector._validate_tag("latest")
        connector._validate_tag("sha-abc123")

    def test_invalid_tags(self, connector):
        with pytest.raises(SkopeoValidationError):
            connector._validate_tag("")
        with pytest.raises(SkopeoValidationError):
            connector._validate_tag(".invalid")  # starts with dot


class TestCommandConstruction:
    def test_build_destination(self, connector):
        with patch("opi.connectors.skopeo.settings") as mock_settings:
            mock_settings.REGISTRY_URL = "rcr.rijksapps.nl"
            mock_settings.REGISTRY_ORG = "rig/zad"
            dest = connector._build_destination("mink", "myapp", "v1")
            assert dest == "docker://rcr.rijksapps.nl/rig/zad:mink_myapp-v1"

    def test_build_command_with_creds(self, connector):
        with patch("opi.connectors.skopeo.settings") as mock_settings:
            mock_settings.REGISTRY_USERNAME = "rig+zad"
            mock_settings.REGISTRY_VERIFY_TLS = True
            cmd = connector._build_command("/tmp/img.tar", "docker://rcr.rijksapps.nl/rig/proj/app:v1")
            assert cmd[0] == "skopeo"
            assert cmd[1] == "copy"
            assert "--dest-creds" in cmd
            creds_idx = cmd.index("--dest-creds")
            assert cmd[creds_idx + 1] == "rig+zad:decrypted-token"
            assert "--dest-tls-verify=false" not in cmd

    def test_build_command_tls_verify_false(self, connector):
        with patch("opi.connectors.skopeo.settings") as mock_settings:
            mock_settings.REGISTRY_USERNAME = "rig+zad"
            mock_settings.REGISTRY_VERIFY_TLS = False
            cmd = connector._build_command("/tmp/img.tar", "docker://dest")
            assert "--dest-tls-verify=false" in cmd

    def test_mask_credentials(self, connector):
        cmd = ["skopeo", "copy", "--dest-creds", "user:secret", "src", "dst"]
        masked = connector._mask_credentials(cmd)
        assert masked[3] == "user:***"
        # Original unchanged
        assert cmd[3] == "user:secret"


class TestPushImage:
    @pytest.mark.asyncio
    async def test_push_success(self, connector):
        mock_process = AsyncMock()
        mock_process.returncode = 0
        mock_process.communicate.return_value = (b"Copying blob sha256:abc\n", b"")

        with (
            patch("asyncio.create_subprocess_exec", return_value=mock_process),
            patch("opi.connectors.skopeo.settings") as mock_settings,
        ):
            mock_settings.REGISTRY_URL = "rcr.rijksapps.nl"
            mock_settings.REGISTRY_ORG = "rig"
            mock_settings.REGISTRY_USERNAME = "rig+zad"
            mock_settings.REGISTRY_VERIFY_TLS = True

            result = await connector.push_image("/tmp/img.tar", "mink", "myapp", "v1.0")
            assert result == "rcr.rijksapps.nl/rig:mink_myapp-v1.0"

    @pytest.mark.asyncio
    async def test_push_fails(self, connector):
        mock_process = AsyncMock()
        mock_process.returncode = 1
        mock_process.communicate.return_value = (b"", b"unauthorized: access denied")

        with (
            patch("asyncio.create_subprocess_exec", return_value=mock_process),
            patch("opi.connectors.skopeo.settings") as mock_settings,
        ):
            mock_settings.REGISTRY_URL = "rcr.rijksapps.nl"
            mock_settings.REGISTRY_ORG = "rig"
            mock_settings.REGISTRY_USERNAME = "rig+zad"
            mock_settings.REGISTRY_VERIFY_TLS = True

            with pytest.raises(SkopeoExecutionError, match="unauthorized"):
                await connector.push_image("/tmp/img.tar", "mink", "app", "v1")

    @pytest.mark.asyncio
    async def test_push_skopeo_unavailable(self, connector):
        connector.is_skopeo_available = False
        with pytest.raises(SkopeoConnectionError):
            await connector.push_image("/tmp/img.tar", "mink", "app", "v1")

    @pytest.mark.asyncio
    async def test_push_registry_not_configured(self, connector):
        with patch("opi.connectors.skopeo.settings") as mock_settings:
            mock_settings.REGISTRY_URL = ""
            with pytest.raises(SkopeoValidationError):
                await connector.push_image("/tmp/img.tar", "mink", "app", "v1")


class TestOwnerPinning:
    """The registry tag carries the project, so one project cannot write another's tag."""

    def test_two_projects_same_image_and_tag_get_different_destinations(self, connector):
        with patch("opi.connectors.skopeo.settings") as mock_settings:
            mock_settings.REGISTRY_URL = "rcr.rijksapps.nl"
            mock_settings.REGISTRY_ORG = "rig/zad"
            dest_a = connector._build_destination("project-a", "backend", "latest")
            dest_b = connector._build_destination("project-b", "backend", "latest")
            assert dest_a == "docker://rcr.rijksapps.nl/rig/zad:project-a_backend-latest"
            assert dest_b == "docker://rcr.rijksapps.nl/rig/zad:project-b_backend-latest"
            assert dest_a != dest_b

    def test_no_image_name_lets_a_project_reach_another_projects_tag(self, connector):
        """A hyphenated project name plus a crafted image name still cannot collide.

        Project 'foo' pushing image 'bar-backend' and project 'foo-bar' pushing
        'backend' would collide if the owner were separated by a hyphen. The owner is
        separated by an underscore, which a project name can never contain, so the
        prefix before the first underscore decides ownership.
        """
        with patch("opi.connectors.skopeo.settings") as mock_settings:
            mock_settings.REGISTRY_URL = "rcr.rijksapps.nl"
            mock_settings.REGISTRY_ORG = "rig"
            assert connector._build_destination("foo", "bar-backend", "latest") != connector._build_destination(
                "foo-bar", "backend", "latest"
            )

    def test_validate_push_target_returns_owned_tag(self, connector):
        assert connector.validate_push_target("mink", "myapp", "v1.0") == "mink_myapp-v1.0"

    def test_invalid_project_names_are_rejected(self, connector):
        with pytest.raises(SkopeoValidationError):
            connector.validate_push_target("", "app", "v1")
        with pytest.raises(SkopeoValidationError):
            connector.validate_push_target("has_underscore", "app", "v1")
        with pytest.raises(SkopeoValidationError):
            connector.validate_push_target("Uppercase", "app", "v1")
        with pytest.raises(SkopeoValidationError):
            connector.validate_push_target("../other", "app", "v1")

    def test_resulting_tag_longer_than_128_is_rejected(self, connector):
        with pytest.raises(SkopeoValidationError, match="at most 128"):
            connector.validate_push_target("mink", "a" * 120, "v1.0")

    @pytest.mark.asyncio
    async def test_push_writes_to_the_owned_destination(self, connector):
        mock_process = AsyncMock()
        mock_process.returncode = 0
        mock_process.communicate.return_value = (b"", b"")

        with (
            patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec,
            patch("opi.connectors.skopeo.settings") as mock_settings,
        ):
            mock_settings.REGISTRY_URL = "rcr.rijksapps.nl"
            mock_settings.REGISTRY_ORG = "rig"
            mock_settings.REGISTRY_USERNAME = "rig+zad"
            mock_settings.REGISTRY_VERIFY_TLS = True

            await connector.push_image("/tmp/img.tar", "project-a", "backend", "latest")

        destination = mock_exec.call_args[0][-1]
        assert destination == "docker://rcr.rijksapps.nl/rig:project-a_backend-latest"

    @pytest.mark.asyncio
    async def test_push_rejects_a_project_name_that_is_not_a_project_name(self, connector):
        with (
            patch("asyncio.create_subprocess_exec") as mock_exec,
            patch("opi.connectors.skopeo.settings") as mock_settings,
        ):
            mock_settings.REGISTRY_URL = "rcr.rijksapps.nl"
            mock_settings.REGISTRY_ORG = "rig"
            with pytest.raises(SkopeoValidationError):
                await connector.push_image("/tmp/img.tar", "not_a_project", "app", "v1")
        mock_exec.assert_not_called()
