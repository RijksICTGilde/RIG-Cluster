"""De CMP leest alleen de sleutel van de app die hij rendert (RC-351).

Twee sidecars op een repo-server: ``cmp-server`` rendert tenant-apps en leest hun sleutel
uit de bestemmingsnamespace, ``cmp-platform`` rendert platform-apps en krijgt de
platformsleutel als gemount bestand. De eerste heeft een token en per projectnamespace
een Role op precies een Secret, de tweede heeft geen token.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from ruamel.yaml import YAML
from tests.cmp_plugin import plugin, render_script
from tests.programma import echt_programma

_REPO_ROOT = Path(__file__).resolve().parents[3]
_COMPONENT = _REPO_ROOT / "bootstrap/rig-system/kustomize/components/sops-plugin/kustomization.yaml"
_OVERLAYS = _REPO_ROOT / "bootstrap/rig-system/kustomize/overlays"

_TENANT_PLUGIN = "kustomize-sops-v1.0"
_PLATFORM_PLUGIN = "kustomize-sops-platform-v1.0"


def _repo_spec() -> dict[str, Any]:
    """De ``spec.repo`` die de component op de ArgoCD-CR zet."""
    component = YAML(typ="safe").load(_COMPONENT.read_text())
    operations = YAML(typ="safe").load(component["patches"][0]["patch"])
    replace = next(op for op in operations if op["path"] == "/spec/repo")
    return replace["value"]


def _sidecar(naam: str) -> dict[str, Any]:
    return next(c for c in _repo_spec()["sidecarContainers"] if c["name"] == naam)


def _mount_paths(container: dict[str, Any]) -> dict[str, str]:
    return {m["name"]: m["mountPath"] for m in container["volumeMounts"]}


def _platform_applications() -> list[tuple[Path, dict[str, Any]]]:
    """De handgeschreven Applications uit beide bootstrap-overlays."""
    gevonden = [
        (pad, YAML(typ="safe").load(pad.read_text())) for pad in sorted(_OVERLAYS.glob("*/argocd-application-*.yaml"))
    ]
    assert gevonden, "geen platform-Applications gevonden"
    return gevonden


def _plugin_env(app: dict[str, Any]) -> tuple[str, dict[str, str]]:
    bron = app["spec"].get("source") or app["spec"]["sources"][0]
    plugin_spec = bron["plugin"]
    return plugin_spec["name"], {e["name"]: e["value"] for e in plugin_spec.get("env", [])}


class TestDeTweeSidecars:
    def test_de_repo_server_draait_onder_een_eigen_serviceaccount(self) -> None:
        """Niet die van de ArgoCD-server: die heeft voor zijn UI veel meer nodig."""
        assert _repo_spec()["serviceaccount"] == "argocd-cmp"

    def test_er_zijn_twee_sidecars_en_cmp_server_staat_voorop(self) -> None:
        """De overlays patchen op index: sidecar 0 moet cmp-server blijven."""
        namen = [c["name"] for c in _repo_spec()["sidecarContainers"]]
        assert namen == ["cmp-server", "cmp-platform"]

    def test_alleen_de_tenant_sidecar_heeft_een_token(self) -> None:
        assert "kube-api-access" in _mount_paths(_sidecar("cmp-server"))
        assert "kube-api-access" not in _mount_paths(_sidecar("cmp-platform"))

    def test_alleen_de_platform_sidecar_ziet_de_sleutel_als_bestand(self) -> None:
        assert _mount_paths(_sidecar("cmp-platform"))["sops-age-key"] == "/sops/sops-age-key"
        assert not [naam for naam in _mount_paths(_sidecar("cmp-server")) if naam.startswith("sops-")]

    def test_elke_sidecar_heeft_zijn_eigen_tmp(self) -> None:
        tmps = {
            naam: pad
            for naam in ("cmp-server", "cmp-platform")
            for pad in [_mount_paths(_sidecar(naam))["cmp-tmp" if naam == "cmp-server" else "cmp-platform-tmp"]]
        }
        assert tmps == {"cmp-server": "/tmp", "cmp-platform": "/tmp"}

    def test_de_sidecars_draaien_elk_hun_eigen_plugin(self) -> None:
        configs = {
            naam: next(
                m["name"] for m in _sidecar(naam)["volumeMounts"] if m["mountPath"] == "/home/argocd/cmp-server/config"
            )
            for naam in ("cmp-server", "cmp-platform")
        }
        assert configs == {"cmp-server": "cmp-plugin", "cmp-platform": "cmp-plugin-platform"}


class TestDePlugins:
    def test_beide_plugins_draaien_hetzelfde_script(self) -> None:
        tenant = plugin()["spec"]["generate"]
        platform = plugin("plugin-platform.yaml")["spec"]["generate"]

        assert tenant["command"] == platform["command"]
        assert tenant["args"] == ["namespace-secret"]
        assert platform["args"] == ["mounted-file"]

    def test_de_plugins_heten_zoals_de_applications_ze_noemen(self) -> None:
        tenant = plugin()
        platform = plugin("plugin-platform.yaml")

        assert f"{tenant['metadata']['name']}-{tenant['spec']['version']}" == _TENANT_PLUGIN
        assert f"{platform['metadata']['name']}-{platform['spec']['version']}" == _PLATFORM_PLUGIN

    def test_het_script_vraagt_de_namespace_niet_meer_op(self) -> None:
        """Een SA die precies een Secret mag lezen, kan geen Namespace-object opvragen."""
        assert "kubectl get namespace" not in render_script()


class TestDePlatformApplications:
    def test_ze_renderen_allemaal_op_de_platform_plugin(self) -> None:
        namen = {pad.name: _plugin_env(app)[0] for pad, app in _platform_applications()}
        assert set(namen.values()) == {_PLATFORM_PLUGIN}, namen

    def test_elke_sleutel_die_ze_noemen_staat_als_mount_in_de_platform_sidecar(self) -> None:
        gemount = {m["name"] for m in _sidecar("cmp-platform")["volumeMounts"]}
        for pad, app in _platform_applications():
            secret = _plugin_env(app)[1]["SOPS_KEY_SECRET"]
            assert secret in gemount, f"{pad.name} noemt {secret}, dat niet in cmp-platform gemount staat"

    def test_tenant_apps_blijven_op_de_tenant_plugin(self) -> None:
        sjabloon = (Path(__file__).parent.parent / "manifests/argocd-application.yaml.jinja").read_text()
        assert f"name: {_TENANT_PLUGIN}" in sjabloon


class TestDeSleutelbronnen:
    """De twee leesfuncties uit het script zelf, zodat ze meeverhuizen als het verandert."""

    def _draai(self, bron: str, tmp_path: Path, kubectl_uitvoer: str | None = None) -> subprocess.CompletedProcess[str]:
        bash = echt_programma("bash")
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        kubectl = bin_dir / "kubectl"
        if kubectl_uitvoer is None:
            kubectl.write_text('#!/bin/bash\necho "Error from server (NotFound)" >&2\nexit 1\n')
        else:
            kubectl.write_text(f'#!/bin/bash\nprintf "%s" "{kubectl_uitvoer}"\n')
        kubectl.chmod(0o755)

        script = render_script()
        kop = script[: script.index("# Function to build Helm chart dependencies")]
        proef = f'{kop}\necho "KEY=$SOPS_AGE_KEY"\n'
        return subprocess.run(
            [bash, "-c", proef, "bash", bron],
            cwd=tmp_path,
            env={"PATH": f"{bin_dir}:/usr/bin:/bin", "ARGOCD_APP_NAMESPACE": "rig-demo"},
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_de_tenant_sidecar_leest_het_secret_uit_de_namespace(self, tmp_path: Path) -> None:
        # base64 van "# key\nAGE-SECRET-KEY-1TEST\n"
        resultaat = self._draai("namespace-secret", tmp_path, "IyBrZXkKQUdFLVNFQ1JFVC1LRVktMVRFU1QK")

        assert resultaat.returncode == 0, resultaat.stderr
        assert "KEY=AGE-SECRET-KEY-1TEST" in resultaat.stdout

    def test_een_onleesbaar_secret_noemt_namespace_en_secret(self, tmp_path: Path) -> None:
        resultaat = self._draai("namespace-secret", tmp_path)

        assert resultaat.returncode != 0
        assert "sops-age-key" in resultaat.stderr
        assert "rig-demo" in resultaat.stderr

    def test_de_platform_sidecar_leest_de_gemounte_sleutel(self, tmp_path: Path) -> None:
        mount = tmp_path / "sops/sops-age-key"
        mount.mkdir(parents=True)
        (mount / "key").write_text("# key\nAGE-SECRET-KEY-1TEST\n")

        bash = echt_programma("bash")
        script = render_script()
        kop = script[: script.index("# Function to build Helm chart dependencies")]
        proef = (
            kop.replace("/sops/${SOPS_KEY_SECRET}/key", f"{tmp_path}/sops/${{SOPS_KEY_SECRET}}/key")
            + '\necho "KEY=$SOPS_AGE_KEY"\n'
        )
        resultaat = subprocess.run(
            [bash, "-c", proef, "bash", "mounted-file"],
            cwd=tmp_path,
            env={"PATH": "/usr/bin:/bin", "ARGOCD_APP_NAMESPACE": "rig-system"},
            capture_output=True,
            text=True,
            timeout=60,
        )

        assert resultaat.returncode == 0, resultaat.stderr
        assert "KEY=AGE-SECRET-KEY-1TEST" in resultaat.stdout

    def test_een_ontbrekende_mount_meldt_dat(self, tmp_path: Path) -> None:
        resultaat = self._draai("mounted-file", tmp_path)

        assert resultaat.returncode != 0
        assert "not mounted" in resultaat.stderr


class TestDeRolBijDeSleutel:
    @pytest.fixture
    def rol_en_binding(self) -> tuple[dict[str, Any], dict[str, Any]]:
        from opi.connectors.kubectl import KubectlConnector

        gerenderd = KubectlConnector().template_manifest(
            (Path(__file__).parent.parent / "manifests/cmp-sops-key-reader.yaml.jinja").read_text(),
            {
                "role_name": "cmp-sops-key-reader",
                "namespace": "rig-demo",
                "secret_name": "sops-age-key",
                "service_account": "argocd-cmp",
                "argo_namespace": "rig-system",
            },
        )
        rol, binding = YAML(typ="safe").load_all(gerenderd)
        return rol, binding

    def test_de_rol_dekt_precies_een_secret_met_precies_get(
        self, rol_en_binding: tuple[dict[str, Any], dict[str, Any]]
    ) -> None:
        rol = rol_en_binding[0]

        assert rol["kind"] == "Role"
        assert rol["metadata"]["namespace"] == "rig-demo"
        assert len(rol["rules"]) == 1
        regel = rol["rules"][0]
        assert regel["resources"] == ["secrets"]
        assert regel["resourceNames"] == ["sops-age-key"]
        assert regel["verbs"] == ["get"]

    def test_de_binding_wijst_naar_de_cmp_in_de_argocd_namespace(
        self, rol_en_binding: tuple[dict[str, Any], dict[str, Any]]
    ) -> None:
        binding = rol_en_binding[1]

        assert binding["roleRef"]["name"] == "cmp-sops-key-reader"
        assert binding["subjects"] == [{"kind": "ServiceAccount", "name": "argocd-cmp", "namespace": "rig-system"}]


class TestDeRolGaatMetHetSecretMee:
    """Alleen waar het secret geplaatst wordt, dus niet bij elke verwerking."""

    @pytest.fixture
    def manager(self, monkeypatch: pytest.MonkeyPatch) -> Any:
        from opi.manager.project_manager import ProjectManager

        pm = ProjectManager(project_file_relative_path="projects/demo.yaml")
        monkeypatch.setattr("opi.manager.project_manager.get_project_public_key", lambda _: "age1demo")
        monkeypatch.setattr(
            "opi.manager.project_manager.get_decoded_project_private_key",
            AsyncMock(return_value="AGE-SECRET-KEY-1DEMO"),
        )
        return pm

    async def test_een_kloppend_secret_levert_geen_enkele_apply_op(
        self, manager: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        kubectl = AsyncMock()
        kubectl.get_sops_secret_from_namespace = AsyncMock(return_value="# public key: age1demo\nAGE-SECRET-KEY-1DEMO")
        monkeypatch.setattr(manager, "_kubectl_connector", kubectl)
        monkeypatch.setattr(manager._sops_handler, "kubectl", kubectl)

        await manager._ensure_sops_secret_in_namespace("rig-demo", {"name": "demo"})

        kubectl.apply_manifest.assert_not_awaited()

    async def test_een_ontbrekend_secret_levert_het_secret_en_de_rol_op(
        self, manager: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        kubectl = AsyncMock()
        kubectl.get_sops_secret_from_namespace = AsyncMock(return_value=None)
        monkeypatch.setattr(manager, "_kubectl_connector", kubectl)
        monkeypatch.setattr(manager._sops_handler, "kubectl", kubectl)

        await manager._ensure_sops_secret_in_namespace("rig-demo", {"name": "demo"})

        sjablonen = [call.args[0] for call in kubectl.apply_manifest.await_args_list]
        assert sjablonen == [
            "manifests/generic-secret.yaml.to-sops.jinja",
            "manifests/cmp-sops-key-reader.yaml.jinja",
        ]
