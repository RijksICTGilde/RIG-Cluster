"""
Tests for the skopeo connector.

Tests singleton pattern, validation, command construction, and push execution
with mocked subprocess calls.
"""

import base64
import ipaddress
import json
import logging
import os
import socket
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.connectors.skopeo import (
    REFUSED_DESTINATION_REASON,
    SkopeoConnectionError,
    SkopeoConnector,
    SkopeoExecutionError,
    SkopeoValidationError,
)


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

    def test_build_command_with_authfile(self, connector):
        with patch("opi.connectors.skopeo.settings") as mock_settings:
            mock_settings.REGISTRY_VERIFY_TLS = True
            cmd = connector._build_command(
                "/tmp/img.tar", "docker://rcr.rijksapps.nl/rig/proj/app:v1", "/tmp/auth/auth.json"
            )
            assert cmd[:4] == ["skopeo", "copy", "--dest-authfile", "/tmp/auth/auth.json"]
            assert "decrypted-token" not in " ".join(cmd)
            assert "--dest-tls-verify=false" not in cmd

    def test_build_command_without_credentials(self, connector):
        with patch("opi.connectors.skopeo.settings") as mock_settings:
            mock_settings.REGISTRY_VERIFY_TLS = True
            cmd = connector._build_command("/tmp/img.tar", "docker://dest", None)
            assert cmd == ["skopeo", "copy", "docker-archive:/tmp/img.tar", "docker://dest"]

    def test_build_command_tls_verify_false(self, connector):
        with patch("opi.connectors.skopeo.settings") as mock_settings:
            mock_settings.REGISTRY_USERNAME = "rig+zad"
            mock_settings.REGISTRY_VERIFY_TLS = False
            cmd = connector._build_command("/tmp/img.tar", "docker://dest", None)
            assert "--dest-tls-verify=false" in cmd


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


PASSWORD = "geheim-token-123"

FAKE_SKOPEO = """#!/bin/sh
tr '\\0' '\\n' < /proc/$$/cmdline > "$SKOPEO_RECORD/cmdline"
while [ $# -gt 0 ]; do
  case "$1" in
    --authfile|--dest-authfile)
      echo "$2" > "$SKOPEO_RECORD/path"
      cp "$2" "$SKOPEO_RECORD/auth.json"
      stat -c %a "$2" > "$SKOPEO_RECORD/mode";;
  esac
  shift
done
echo "unauthorized: authentication required" >&2
exit "$SKOPEO_EXIT"
"""


@pytest.fixture
def fake_skopeo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Een echte ``skopeo`` op PATH die zijn argv en authfile vastlegt."""
    bin_dir = tmp_path / "bin"
    record = tmp_path / "record"
    bin_dir.mkdir()
    record.mkdir()
    script = bin_dir / "skopeo"
    script.write_text(FAKE_SKOPEO)
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("SKOPEO_RECORD", str(record))
    monkeypatch.setenv("SKOPEO_EXIT", "0")
    return record


#: Wat de nepresolver per naam teruggeeft; IP-letterlijken gaan naar de echte.
DNS = {
    "localhost": ["127.0.0.1", "::1"],
    "ghcr.io": ["140.82.112.33"],
    "code.overheid.nl": ["145.21.1.1", "2a00:1450:4001:80b::2004"],
    "kubernetes.default.svc": ["10.96.0.1"],
    "registry.voorbeeld.nl": ["10.0.0.5"],
    "half.voorbeeld.nl": ["140.82.112.34", "192.168.1.10"],
    "registry.sandbox.rijksapp.dev": ["10.96.12.34"],
    "ula.voorbeeld.nl": ["fd00::1"],
    "linklocal6.voorbeeld.nl": ["fe80::1%eth0"],
    "mapped.voorbeeld.nl": ["::ffff:10.0.0.5"],
}
_REAL_GETADDRINFO = socket.getaddrinfo


def _fake_getaddrinfo(host: str, port: object, *args: object, **kwargs: object) -> list:
    host.encode("idna")  # zoals de echte: een label boven 63 tekens geeft UnicodeError
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if host not in DNS:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known") from None
        return [
            (
                socket.AF_INET6 if ":" in ip else socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                (ip, 0, 0, 0) if ":" in ip else (ip, 0),
            )
            for ip in DNS[host]
        ]
    return _REAL_GETADDRINFO(host, port, *args, **kwargs)


@pytest.fixture
def resolver():
    with patch("socket.getaddrinfo", side_effect=_fake_getaddrinfo) as fake:
        yield fake


@pytest.mark.asyncio
class TestDestinationGuard:
    @pytest.mark.parametrize(
        "repository",
        [
            "169.254.169.254/latest",
            "127.0.0.1:22/x",
            "10.43.0.1:8080/x",
            "kubernetes.default.svc/x",
            "0.0.0.0:9595/x",
            "100.64.0.1:5000/x",
            "localhost:9595/x",
        ],
    )
    async def test_private_and_special_destinations_are_refused_with_one_message(
        self, connector, resolver, fake_skopeo, repository
    ):
        with patch("asyncio.create_subprocess_exec") as mock_exec:
            result = await connector.check_repository_access(repository, "robbert", PASSWORD)
        assert result == (False, REFUSED_DESTINATION_REASON)
        mock_exec.assert_not_called()

    @pytest.mark.parametrize("repository", ["ghcr.io/iets", "code.overheid.nl/iets"])
    async def test_a_public_registry_is_checked(self, connector, resolver, fake_skopeo, repository):
        assert await connector.check_repository_access(repository, "robbert", PASSWORD) == (True, "")
        assert (fake_skopeo / "cmdline").read_text().splitlines()[-1] == f"docker://{repository}"

    async def test_a_public_looking_name_that_resolves_privately_is_refused(self, connector, resolver, fake_skopeo):
        """Een oordeel op de tekst alleen laat deze door."""
        result = await connector.check_repository_access("registry.voorbeeld.nl/app", "robbert", PASSWORD)
        assert result == (False, REFUSED_DESTINATION_REASON)
        assert not (fake_skopeo / "cmdline").exists()

    async def test_one_private_address_among_public_ones_is_enough(self, connector, resolver, fake_skopeo):
        result = await connector.check_repository_access("half.voorbeeld.nl/app", "robbert", PASSWORD)
        assert result == (False, REFUSED_DESTINATION_REASON)

    @pytest.mark.parametrize("host", ["ula.voorbeeld.nl", "linklocal6.voorbeeld.nl", "mapped.voorbeeld.nl"])
    async def test_a_name_that_resolves_to_private_ipv6_is_refused(self, connector, resolver, fake_skopeo, host):
        result = await connector.check_repository_access(f"{host}/app", "robbert", PASSWORD)
        assert result == (False, REFUSED_DESTINATION_REASON)

    async def test_a_host_the_resolver_cannot_encode_gets_the_same_message(self, connector, resolver, fake_skopeo):
        """``UPSTREAM_PATTERN`` begrenst de labellengte niet."""
        result = await connector.check_repository_access(f"{'a' * 64}.nl/app", "robbert", PASSWORD)
        assert result == (False, REFUSED_DESTINATION_REASON)

    async def test_a_name_that_does_not_resolve_gets_the_same_message(self, connector, resolver, fake_skopeo):
        """Anders verklapt het verschil welke interne namen bestaan."""
        result = await connector.check_repository_access("bestaat.niet.intern/app", "robbert", PASSWORD)
        assert result == (False, REFUSED_DESTINATION_REASON)

    async def test_the_platform_registry_is_allowed_in_the_sandbox(self, connector, resolver, fake_skopeo):
        with patch("opi.connectors.skopeo.settings") as mock_settings:
            mock_settings.CLUSTER_MANAGER = "sandboxed-local"
            mock_settings.REGISTRY_URL = "registry.sandbox.rijksapp.dev"
            result = await connector.check_repository_access("registry.sandbox.rijksapp.dev/rig/app", "admin", PASSWORD)
        assert result == (True, "")

    async def test_the_sandbox_exception_holds_only_for_the_platform_registry(self, connector, resolver, fake_skopeo):
        with patch("opi.connectors.skopeo.settings") as mock_settings:
            mock_settings.CLUSTER_MANAGER = "sandboxed-local"
            mock_settings.REGISTRY_URL = "registry.sandbox.rijksapp.dev"
            result = await connector.check_repository_access("kubernetes.default.svc/x", "admin", PASSWORD)
        assert result == (False, REFUSED_DESTINATION_REASON)

    async def test_the_sandbox_exception_holds_only_in_the_sandbox(self, connector, resolver, fake_skopeo):
        with patch("opi.connectors.skopeo.settings") as mock_settings:
            mock_settings.CLUSTER_MANAGER = "odcn-production"
            mock_settings.REGISTRY_URL = "registry.sandbox.rijksapp.dev"
            result = await connector.check_repository_access("registry.sandbox.rijksapp.dev/rig/app", "admin", PASSWORD)
        assert result == (False, REFUSED_DESTINATION_REASON)


def _assert_authfile_used_and_gone(record: Path, registry: str, username: str) -> None:
    """Skopeo zoekt de credentials op de host als sleutel."""
    cmdline = (record / "cmdline").read_text()
    assert PASSWORD not in cmdline
    assert username not in cmdline
    assert (record / "mode").read_text().strip() == "600"
    auth = (record / "auth.json").read_text()
    assert PASSWORD not in auth
    assert base64.b64decode(json.loads(auth)["auths"][registry]["auth"]).decode() == f"{username}:{PASSWORD}"
    path = Path((record / "path").read_text().strip())
    assert not path.exists()
    assert not path.parent.exists()


@pytest.mark.asyncio
class TestCredentialsStayOutOfArgv:
    @pytest.mark.parametrize("exit_code", ["0", "1"])
    async def test_list_tags(self, connector, resolver, fake_skopeo, monkeypatch, exit_code):
        monkeypatch.setenv("SKOPEO_EXIT", exit_code)
        ok, reason = await connector.check_repository_access("ghcr.io/team/app", "robbert", PASSWORD)
        assert ok is (exit_code == "0")
        _assert_authfile_used_and_gone(fake_skopeo, "ghcr.io", "robbert")
        assert "--authfile" in (fake_skopeo / "cmdline").read_text()

    @pytest.mark.parametrize("exit_code", ["0", "1"])
    async def test_push(self, connector, fake_skopeo, monkeypatch, exit_code):
        monkeypatch.setenv("SKOPEO_EXIT", exit_code)
        connector._registry_password = PASSWORD
        with patch("opi.connectors.skopeo.settings") as mock_settings:
            mock_settings.REGISTRY_URL = "rcr.rijksapps.nl"
            mock_settings.REGISTRY_ORG = "rig"
            mock_settings.REGISTRY_USERNAME = "rig+zad"
            mock_settings.REGISTRY_VERIFY_TLS = True
            if exit_code == "0":
                await connector.push_image("/tmp/img.tar", "mink", "app", "v1")
            else:
                with pytest.raises(SkopeoExecutionError, match="unauthorized"):
                    await connector.push_image("/tmp/img.tar", "mink", "app", "v1")
        _assert_authfile_used_and_gone(fake_skopeo, "rcr.rijksapps.nl", "rig+zad")
        assert "--dest-authfile" in (fake_skopeo / "cmdline").read_text()

    async def test_push_without_credentials_passes_no_authfile(self, connector, fake_skopeo):
        connector._registry_password = None
        with patch("opi.connectors.skopeo.settings") as mock_settings:
            mock_settings.REGISTRY_URL = "rcr.rijksapps.nl"
            mock_settings.REGISTRY_ORG = "rig"
            mock_settings.REGISTRY_USERNAME = "rig+zad"
            mock_settings.REGISTRY_VERIFY_TLS = True
            await connector.push_image("/tmp/img.tar", "mink", "app", "v1")
        assert not (fake_skopeo / "path").exists()

    async def test_the_authfile_is_removed_when_the_process_cannot_start(self, connector, resolver, tmp_path):
        seen: list[str] = []

        async def exploding_exec(*cmd: str, **kwargs: object) -> None:
            seen.append(cmd[cmd.index("--authfile") + 1])
            raise PermissionError("no exec")

        with (
            patch("asyncio.create_subprocess_exec", side_effect=exploding_exec),
            pytest.raises(PermissionError),
        ):
            await connector.check_repository_access("ghcr.io/team/app", "robbert", PASSWORD)
        assert seen
        assert not Path(seen[0]).exists()

    async def test_the_log_carries_no_password(self, connector, resolver, fake_skopeo, caplog):
        with caplog.at_level(logging.DEBUG, logger="opi.connectors.skopeo"):
            await connector.check_repository_access("ghcr.io/team/app", "robbert", PASSWORD)
            connector._registry_password = PASSWORD
            with patch("opi.connectors.skopeo.settings") as mock_settings:
                mock_settings.REGISTRY_URL = "rcr.rijksapps.nl"
                mock_settings.REGISTRY_ORG = "rig"
                mock_settings.REGISTRY_USERNAME = "rig+zad"
                mock_settings.REGISTRY_VERIFY_TLS = True
                await connector.push_image("/tmp/img.tar", "mink", "app", "v1")
        assert "Verifying registry access" in caplog.text
        assert "Pushing image" in caplog.text
        assert PASSWORD not in caplog.text
