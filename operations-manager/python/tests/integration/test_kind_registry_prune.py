"""scripts/prune-kind-registry.sh tegen een echte registry:2-container.

De beloftes hangen aan wat `registry garbage-collect` echt doet met gedeelde lagen en
met een index van buildx, dus een stub bewijst ze niet. De images worden via de
registry-API zelf opgebouwd, zodat het bouwmoment in de config vrij te kiezen is.
"""

import hashlib
import json
import os
import shutil
import socket
import subprocess
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.requires_infra

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT = REPO_ROOT / "scripts" / "prune-kind-registry.sh"
REGISTRY_IMAGE = "registry:2"
REPO = "operations-manager"

OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
OCI_INDEX = "application/vnd.oci.image.index.v1+json"
OCI_CONFIG = "application/vnd.oci.image.config.v1+json"
OCI_LAYER = "application/vnd.oci.image.layer.v1.tar+gzip"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _docker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=True, timeout=120)


class Registry:
    def __init__(self, name: str, port: int) -> None:
        self.name = name
        self.port = port
        self.url = f"http://127.0.0.1:{port}"
        self.repo = REPO

    def _request(
        self, method: str, path: str, data: bytes | None = None, headers: dict | None = None
    ) -> tuple[int, dict, bytes]:
        request = urllib.request.Request(f"{self.url}{path}", data=data, method=method, headers=headers or {})  # noqa: S310 (vaste 127.0.0.1)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 (vaste 127.0.0.1)
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as error:
            return error.code, dict(error.headers), error.read()

    def push_blob(self, content: bytes) -> dict:
        digest = "sha256:" + hashlib.sha256(content).hexdigest()
        _, headers, _ = self._request("POST", f"/v2/{self.repo}/blobs/uploads/")
        location = headers["Location"]
        separator = "&" if "?" in location else "?"
        path = location.removeprefix(self.url) + f"{separator}digest={digest}"
        status, _, body = self._request("PUT", path, data=content, headers={"Content-Type": "application/octet-stream"})
        assert status == 201, body
        return {"digest": digest, "size": len(content)}

    def push_manifest(self, reference: str, media_type: str, manifest: dict) -> dict:
        content = json.dumps(manifest).encode()
        status, headers, body = self._request(
            "PUT", f"/v2/{self.repo}/manifests/{reference}", data=content, headers={"Content-Type": media_type}
        )
        assert status == 201, body
        return {"mediaType": media_type, "digest": headers["Docker-Content-Digest"], "size": len(content)}

    def push_image(self, tag: str, created: datetime, layers: list[bytes], with_attestation: bool) -> dict:
        """Een image zoals buildx hem pusht: met attestation als index, anders een los manifest."""
        config = json.dumps({"created": created.isoformat(), "architecture": "amd64", "os": "linux", "salt": tag})
        config_ref = self.push_blob(config.encode()) | {"mediaType": OCI_CONFIG}
        layer_refs = [self.push_blob(layer) | {"mediaType": OCI_LAYER} for layer in layers]
        image = {"schemaVersion": 2, "mediaType": OCI_MANIFEST, "config": config_ref, "layers": layer_refs}
        if not with_attestation:
            return self.push_manifest(tag, OCI_MANIFEST, image) | {"layers": layer_refs}

        image_ref = self.push_manifest(image_digest_ref(image), OCI_MANIFEST, image)
        statement = self.push_blob(json.dumps({"subject": image_ref["digest"], "salt": tag}).encode())
        empty = self.push_blob(b"{}") | {"mediaType": "application/vnd.oci.empty.v1+json"}
        attestation = {
            "schemaVersion": 2,
            "mediaType": OCI_MANIFEST,
            "config": empty,
            "layers": [statement | {"mediaType": "application/vnd.in-toto+json"}],
        }
        attestation_ref = self.push_manifest(image_digest_ref(attestation), OCI_MANIFEST, attestation)
        index = {
            "schemaVersion": 2,
            "mediaType": OCI_INDEX,
            "manifests": [
                image_ref | {"platform": {"architecture": "amd64", "os": "linux"}},
                attestation_ref
                | {
                    "platform": {"architecture": "unknown", "os": "unknown"},
                    "annotations": {"vnd.docker.reference.type": "attestation-manifest"},
                },
            ],
        }
        return self.push_manifest(tag, OCI_INDEX, index) | {
            "layers": layer_refs,
            "children": [image_ref["digest"], attestation_ref["digest"]],
        }

    def tags(self) -> list[str]:
        _, _, body = self._request("GET", f"/v2/{self.repo}/tags/list")
        return sorted(json.loads(body).get("tags") or [])

    def stored(self, digest: str) -> bool:
        """Staat de blob nog in het volume.

        Niet via de API: een HEAD geeft na de garbage collect nog 200 uit de cache van de
        registry, ook als de data al weg is.
        """
        hexdigest = digest.removeprefix("sha256:")
        path = f"/var/lib/registry/docker/registry/v2/blobs/sha256/{hexdigest[:2]}/{hexdigest}/data"
        return subprocess.run(["docker", "exec", self.name, "test", "-f", path]).returncode == 0

    def pullable(self, digest: str) -> bool:
        accept = f"{OCI_INDEX},{OCI_MANIFEST}"
        status, _, body = self._request("GET", f"/v2/{self.repo}/manifests/{digest}", headers={"Accept": accept})
        if status != 200:
            return False
        manifest = json.loads(body)
        blobs = [manifest["config"], *manifest["layers"]] if "config" in manifest else []
        return all(self._request("GET", f"/v2/{self.repo}/blobs/{blob['digest']}")[0] == 200 for blob in blobs)


def image_digest_ref(manifest: dict) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(manifest).encode()).hexdigest()


def _registry_container(delete_enabled: bool) -> Iterator[Registry]:
    if shutil.which("docker") is None or shutil.which("jq") is None:
        pytest.skip("docker of jq ontbreekt")
    if subprocess.run(["docker", "image", "inspect", REGISTRY_IMAGE], capture_output=True).returncode != 0:
        pytest.skip(f"{REGISTRY_IMAGE} staat niet lokaal")
    name = f"prune-proef-{uuid.uuid4().hex[:8]}"
    port = _free_port()
    env = ["-e", "REGISTRY_STORAGE_DELETE_ENABLED=true"] if delete_enabled else []
    _docker("run", "-d", "-p", f"127.0.0.1:{port}:5000", *env, "--name", name, REGISTRY_IMAGE)
    registry = Registry(name, port)
    try:
        for _ in range(100):
            try:
                if registry._request("GET", "/v2/")[0] == 200:
                    break
            except OSError:
                pass
            subprocess.run(["sleep", "0.1"], check=True)
        yield registry
    finally:
        subprocess.run(["docker", "rm", "-f", "-v", name], capture_output=True)


@pytest.fixture
def registry() -> Iterator[Registry]:
    yield from _registry_container(delete_enabled=True)


@pytest.fixture
def registry_without_delete() -> Iterator[Registry]:
    yield from _registry_container(delete_enabled=False)


def _prune(registry: Registry, **env: str) -> subprocess.CompletedProcess[str]:
    return _run_script(registry, os.environ["PATH"], **env)


def _run_script(registry: Registry, path: str, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=120,
        env={
            **os.environ,
            "PATH": path,
            "KIND_REGISTRY_NAME": registry.name,
            "KIND_REGISTRY_PORT": str(registry.port),
            **env,
        },
    )


NOW = datetime.now(UTC)
BASE_LAYER = os.urandom(2 * 1024 * 1024)


def _push_history(registry: Registry, with_attestation: bool) -> dict[str, dict]:
    """Drie builds op dezelfde basislaag: twee oud, een van een uur geleden."""
    return {
        tag: registry.push_image(tag, created, [BASE_LAYER, os.urandom(64 * 1024)], with_attestation)
        for tag, created in (
            ("oud-1", NOW - timedelta(days=5)),
            ("oud-2", NOW - timedelta(days=3)),
            ("nieuw", NOW - timedelta(hours=1)),
        )
    }


@pytest.mark.parametrize("with_attestation", [True, False], ids=["index", "manifest"])
class TestPrune:
    def test_old_tags_go_and_the_recent_one_stays(self, registry: Registry, with_attestation: bool) -> None:
        _push_history(registry, with_attestation)

        result = _prune(registry)

        assert result.returncode == 0, result.stderr
        assert registry.tags() == ["nieuw"]
        assert "omvang voor: " in result.stdout
        assert "omvang na: " in result.stdout

    def test_the_app_layers_of_old_builds_are_collected(self, registry: Registry, with_attestation: bool) -> None:
        images = _push_history(registry, with_attestation)

        _prune(registry)

        for tag in ("oud-1", "oud-2"):
            assert not registry.stored(images[tag]["layers"][1]["digest"]), tag
            for manifest in (images[tag]["digest"], *images[tag].get("children", [])):
                assert not registry.stored(manifest), tag

    def test_the_shared_base_layer_stays(self, registry: Registry, with_attestation: bool) -> None:
        images = _push_history(registry, with_attestation)

        _prune(registry)

        assert registry.stored(images["nieuw"]["layers"][0]["digest"])

    def test_the_kept_image_is_still_complete(self, registry: Registry, with_attestation: bool) -> None:
        """Een garbage collect die de kinderen van een index meeneemt, breekt de nieuwste pull."""
        images = _push_history(registry, with_attestation)

        _prune(registry, RETENTIE_DAGEN="0")

        kept = images["nieuw"]
        for manifest in (kept["digest"], *kept.get("children", [])):
            assert registry.stored(manifest)
            assert registry.pullable(manifest)
        for layer in kept["layers"]:
            assert registry.stored(layer["digest"])

    def test_zero_days_keeps_only_the_newest(self, registry: Registry, with_attestation: bool) -> None:
        _push_history(registry, with_attestation)

        result = _prune(registry, RETENTIE_DAGEN="0")

        assert result.returncode == 0, result.stderr
        assert registry.tags() == ["nieuw"]

    def test_a_long_retention_keeps_everything(self, registry: Registry, with_attestation: bool) -> None:
        _push_history(registry, with_attestation)

        _prune(registry, RETENTIE_DAGEN="7")

        assert registry.tags() == ["nieuw", "oud-1", "oud-2"]

    def test_the_newest_stays_even_when_everything_is_old(self, registry: Registry, with_attestation: bool) -> None:
        for tag, days in (("a", 9), ("b", 8)):
            registry.push_image(tag, NOW - timedelta(days=days), [BASE_LAYER, os.urandom(1024)], with_attestation)

        _prune(registry)

        assert registry.tags() == ["b"]


def test_each_repository_keeps_its_own_newest(registry: Registry) -> None:
    """De nieuwste van de ene repository mag de nieuwste van de andere niet overschaduwen."""
    for repo, tags in (("operations-manager", (("a", 9), ("b", 8))), ("ander", (("c", 6), ("d", 5)))):
        registry.repo = repo
        for tag, days in tags:
            registry.push_image(tag, NOW - timedelta(days=days), [BASE_LAYER, os.urandom(1024)], with_attestation=True)

    result = _prune(registry)

    assert result.returncode == 0, result.stderr
    registry.repo = "operations-manager"
    assert registry.tags() == ["b"]
    registry.repo = "ander"
    assert registry.tags() == ["d"]


def test_a_tag_without_build_moment_stays(registry: Registry) -> None:
    """Zonder `created` is de leeftijd onbekend; weggooien zou op een gok gebeuren."""
    config = registry.push_blob(json.dumps({"architecture": "amd64", "os": "linux"}).encode())
    registry.push_manifest(
        "zonder-moment",
        OCI_MANIFEST,
        {"schemaVersion": 2, "mediaType": OCI_MANIFEST, "config": config | {"mediaType": OCI_CONFIG}, "layers": []},
    )
    registry.push_image("oud", NOW - timedelta(days=5), [BASE_LAYER], with_attestation=False)
    registry.push_image("nieuw", NOW - timedelta(hours=1), [BASE_LAYER], with_attestation=False)

    result = _prune(registry)

    assert result.returncode == 0, result.stderr
    assert registry.tags() == ["nieuw", "zonder-moment"]
    assert "zonder-moment heeft geen leesbaar bouwmoment" in result.stdout


def test_a_registry_nobody_pushed_to_is_nothing_to_prune(registry: Registry) -> None:
    """Zo staat hij na sandbox:setup, en het opruimen is de laatste stap van elke deploy."""
    result = _prune(registry)

    assert result.returncode == 0, result.stderr


def test_two_tags_on_one_digest_are_deleted_once(registry: Registry) -> None:
    """Een delete op digest haalt alle tags ervan weg; een tweede delete gaf 404 en stopte het script."""
    registry.push_image("oud", NOW - timedelta(days=5), [BASE_LAYER], with_attestation=True)
    _, _, body = registry._request("GET", f"/v2/{registry.repo}/manifests/oud", headers={"Accept": OCI_INDEX})
    status, _, _ = registry._request(
        "PUT", f"/v2/{registry.repo}/manifests/oud-alias", data=body, headers={"Content-Type": OCI_INDEX}
    )
    assert status == 201
    registry.push_image("nieuw", NOW, [BASE_LAYER], with_attestation=True)

    result = _prune(registry)

    assert result.returncode == 0, result.stderr
    assert registry.tags() == ["nieuw"]


def test_refuses_a_registry_without_delete(registry_without_delete: Registry) -> None:
    registry_without_delete.push_image("oud", NOW - timedelta(days=5), [BASE_LAYER], with_attestation=False)

    result = _prune(registry_without_delete)

    assert result.returncode == 4
    assert "sandbox:setup-registry" in result.stderr
    assert registry_without_delete.tags() == ["oud"]


def test_only_the_registry_container_is_touched(registry: Registry, tmp_path: Path) -> None:
    """De buildcache leeft in het volume van de builder; het opruimen blijft daarvan af."""
    real_docker = shutil.which("docker")
    log = tmp_path / "docker.log"
    wrapper = tmp_path / "docker"
    wrapper.write_text(f'#!/usr/bin/env bash\necho "$*" >> {log}\nexec {real_docker} "$@"\n')
    wrapper.chmod(0o755)
    _push_history(registry, with_attestation=True)

    result = _run_script(registry, f"{tmp_path}:{os.environ['PATH']}", RETENTIE_DAGEN="0")

    assert result.returncode == 0, result.stderr
    calls = log.read_text().splitlines()
    assert calls
    for call in calls:
        assert call.split()[0] in {"inspect", "exec"}, call
        assert registry.name in call.split(), call
