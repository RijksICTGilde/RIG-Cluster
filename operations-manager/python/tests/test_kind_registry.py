"""Tests voor de registry die naast het kind-cluster staat.

Vier dingen worden hier bewaakt:
  1. scripts/setup-kind-registry.sh - de stappen van de kind-recipe, en de weigering als
     containerd de certs.d-map niet leest.
  2. sandboxed-local/kind-config.yaml - de containerdConfigPatches die dat mogelijk maken.
     Valt die weg, dan negeert containerd de hosts.toml en pullt de node stilletjes niets.
  3. Taskfile - sandbox:setup moet het script aanroepen na het cluster en voor alles wat
     een image nodig heeft.
  4. De verwijzingen naar docs/sandbox-kind-registry.md. Het script en de kind-config
     leggen niets meer zelf uit, dus een hernoemde doc laat de weigering naar niets wijzen.

De shell-stappen worden gemeten met stubs voor docker, kind en kubectl die hun aanroepen
wegschrijven. Zo is de volgorde en de inhoud toetsbaar zonder een echt cluster.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "setup-kind-registry.sh"
KIND_CONFIG = REPO_ROOT / "sandboxed-local" / "kind-config.yaml"
TASKFILE = REPO_ROOT / "Taskfile.yaml"
DOC = REPO_ROOT / "docs" / "sandbox-kind-registry.md"

REGISTRY_TASK = "sandbox:setup-registry"
CREATE_CLUSTER_TASK = "sandbox:create-cluster"

DOCKER_STUB = """#!/usr/bin/env bash
echo "docker $*" >> "$STUB_LOG"
case "$1" in
  run)
    [ -z "${STUB_RUN_FAILS:-}" ] || { echo "docker: port is already allocated" >&2; exit 125; }
    ;;
  inspect)
    case "$*" in
      *State.Running*)
        [ -n "${STUB_REG_STATE:-}" ] || exit 1
        echo "$STUB_REG_STATE"
        ;;
      *NetworkSettings.Networks.kind*)
        echo "${STUB_REG_NETWORK:-null}"
        ;;
    esac
    ;;
  exec)
    case "$*" in
      *config.toml*)
        [ -z "${STUB_EXEC_FAILS:-}" ] || { echo "Error response from daemon: container is not running" >&2; exit "$STUB_EXEC_FAILS"; }
        cat "$STUB_CONFIGS/${STUB_CONFIG_PATH:-yes}.toml"
        ;;
      *"cp /dev/stdin"*)
        cat >> "$STUB_HOSTS_TOML"
        ;;
    esac
    ;;
esac
exit 0
"""

KIND_STUB = """#!/usr/bin/env bash
echo "kind $*" >> "$STUB_LOG"
[ -z "${STUB_KIND_FAILS:-}" ] || { echo "ERROR: failed to list nodes" >&2; exit "$STUB_KIND_FAILS"; }
if [ "$1" = "get" ] && [ "$2" = "nodes" ]; then
  for node in ${STUB_NODES-proef-control-plane}; do echo "$node"; done
fi
exit 0
"""

# Uitsnede van /etc/containerd/config.toml op een ongepatchte kind-node (rig-sandbox,
# kindest/node v1.32.0). Het woord registry staat er al in, via sandbox_image.
UNPATCHED_CONFIG = """# explicitly use v2 config format
version = 2

[plugins."io.containerd.grpc.v1.cri".containerd]
  snapshotter = "overlayfs"
  default_runtime_name = "runc"

[plugins."io.containerd.grpc.v1.cri"]
  # use fixed sandbox image
  sandbox_image = "registry.k8s.io/pause:3.10"
  restrict_oom_score_adj = false
"""

CONTAINERD_CONFIGS = {
    "yes": UNPATCHED_CONFIG
    + """
[plugins."io.containerd.grpc.v1.cri".registry]
  config_path = "/etc/containerd/certs.d"
""",
    "no": UNPATCHED_CONFIG,
    "commented": UNPATCHED_CONFIG
    + """
[plugins."io.containerd.grpc.v1.cri".registry]
  # config_path = "/etc/containerd/certs.d"
""",
}

KUBECTL_STUB = """#!/usr/bin/env bash
echo "kubectl $*" >> "$STUB_LOG"
cat >> "$STUB_KUBECTL_STDIN"
exit 0
"""


class Run:
    """Uitkomst van een scriptrun plus wat de stubs opvingen."""

    def __init__(self, proc: subprocess.CompletedProcess[str], workdir: Path) -> None:
        self.proc = proc
        self.returncode = proc.returncode
        self.stdout = proc.stdout
        self.stderr = proc.stderr
        self.log = (workdir / "calls.log").read_text() if (workdir / "calls.log").exists() else ""
        hosts = workdir / "hosts.toml"
        self.hosts_toml = hosts.read_text() if hosts.exists() else ""
        cm = workdir / "kubectl-stdin"
        self.kubectl_stdin = cm.read_text() if cm.exists() else ""

    def calls(self) -> list[str]:
        return [line for line in self.log.splitlines() if line]


def _run(tmp_path: Path, *args: str, **env_extra: str) -> Run:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in (("docker", DOCKER_STUB), ("kind", KIND_STUB), ("kubectl", KUBECTL_STUB)):
        stub = bindir / name
        stub.write_text(body)
        stub.chmod(0o755)
    configs = tmp_path / "configs"
    configs.mkdir()
    for name, body in CONTAINERD_CONFIGS.items():
        (configs / f"{name}.toml").write_text(body)

    env = {
        "PATH": f"{bindir}:/usr/bin:/bin:/usr/local/bin",
        "STUB_LOG": str(tmp_path / "calls.log"),
        "STUB_HOSTS_TOML": str(tmp_path / "hosts.toml"),
        "STUB_KUBECTL_STDIN": str(tmp_path / "kubectl-stdin"),
        "STUB_CONFIGS": str(configs),
        **env_extra,
    }
    proc = subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    return Run(proc, tmp_path)


@pytest.fixture(scope="module")
def taskfile() -> dict:
    return yaml.safe_load(TASKFILE.read_text())


@pytest.fixture(scope="module")
def kind_config() -> dict:
    return yaml.safe_load(KIND_CONFIG.read_text())


@pytest.fixture(scope="module")
def bash_available() -> None:
    if shutil.which("bash") is None:
        pytest.skip("bash not available")


@pytest.mark.usefixtures("bash_available")
class TestSetupScript:
    def test_script_is_executable(self) -> None:
        assert SCRIPT.stat().st_mode & 0o111

    def test_starts_the_registry_on_the_loopback_address(self, tmp_path: Path) -> None:
        run = _run(tmp_path, "--cluster", "proef")

        assert run.returncode == 0, run.stderr
        assert "docker run -d --restart=always -p 127.0.0.1:5001:5000" in run.log
        assert "--name kind-registry registry:2" in run.log

    def test_does_not_start_a_registry_that_already_runs(self, tmp_path: Path) -> None:
        run = _run(tmp_path, "--cluster", "proef", STUB_REG_STATE="true")

        assert run.returncode == 0, run.stderr
        assert "docker run" not in run.log
        assert "docker start" not in run.log

    def test_starts_a_registry_that_exists_but_is_stopped(self, tmp_path: Path) -> None:
        """`docker run` met dezelfde naam loopt stuk op de bestaande container."""
        run = _run(tmp_path, "--cluster", "proef", STUB_REG_STATE="false")

        assert run.returncode == 0, run.stderr
        assert "docker start kind-registry" in run.log
        assert "docker run" not in run.log

    def test_writes_hosts_toml_on_every_node(self, tmp_path: Path) -> None:
        run = _run(tmp_path, "--cluster", "proef", STUB_NODES="proef-control-plane proef-worker")

        assert run.returncode == 0, run.stderr
        for node in ("proef-control-plane", "proef-worker"):
            assert f"docker exec {node} mkdir -p /etc/containerd/certs.d/localhost:5001" in run.log
            assert f"docker exec -i {node} cp /dev/stdin /etc/containerd/certs.d/localhost:5001/hosts.toml" in run.log

    def test_hosts_toml_points_at_the_container_name(self, tmp_path: Path) -> None:
        """De node bereikt de registry over het kind-netwerk, niet op zijn eigen localhost."""
        run = _run(tmp_path, "--cluster", "proef")

        assert run.hosts_toml.strip() == '[host."http://kind-registry:5000"]'

    def test_refuses_when_containerd_ignores_certs_d(self, tmp_path: Path) -> None:
        """De stille fout: zonder config_path leest containerd de hosts.toml nooit."""
        run = _run(tmp_path, "--cluster", "proef", STUB_CONFIG_PATH="no")

        assert run.returncode == 4
        assert "containerdConfigPatches" in run.stderr
        assert "kind-config" in run.stderr

    def test_refuses_a_config_path_that_is_commented_out(self, tmp_path: Path) -> None:
        run = _run(tmp_path, "--cluster", "proef", STUB_CONFIG_PATH="commented")

        assert run.returncode == 4
        assert run.hosts_toml == ""

    @pytest.mark.parametrize("exec_code", ["1", "125"])
    def test_a_node_that_cannot_be_read_is_no_missing_patch(self, tmp_path: Path, exec_code: str) -> None:
        """Een gestopte node gaf eerst het advies de gedeelde sandbox te herbouwen.

        125 naast 1: de kop belooft de code van docker, niet een vaste code.
        """
        run = _run(tmp_path, "--cluster", "proef", STUB_EXEC_FAILS=exec_code)

        assert run.returncode == int(exec_code)
        assert "not running" in run.stderr
        assert "containerdConfigPatches" not in run.stderr
        assert run.hosts_toml == ""
        assert "kubectl" not in run.log

    def test_refusal_writes_no_hosts_toml(self, tmp_path: Path) -> None:
        run = _run(tmp_path, "--cluster", "proef", STUB_CONFIG_PATH="no")

        assert run.hosts_toml == ""

    def test_refuses_a_cluster_without_nodes(self, tmp_path: Path) -> None:
        """kind get nodes geeft exit 0 voor een onbekend cluster; zonder weigering viel het script pas op kubectl om."""
        run = _run(tmp_path, "--cluster", "bestaat-niet", STUB_NODES="")

        assert run.returncode == 3
        assert "bestaat-niet" in run.stderr
        assert "docker run" not in run.log
        assert "kubectl" not in run.log

    def test_stops_with_the_code_of_kind_when_listing_nodes_fails(self, tmp_path: Path) -> None:
        """In een for-lijst slikte bash de fout van kind in; als toewijzing geeft set -e hem door."""
        run = _run(tmp_path, "--cluster", "proef", STUB_KIND_FAILS="7")

        assert run.returncode == 7
        assert "failed to list nodes" in run.stderr
        assert "docker run" not in run.log

    def test_connects_the_registry_to_the_kind_network(self, tmp_path: Path) -> None:
        run = _run(tmp_path, "--cluster", "proef")

        assert "docker network connect kind kind-registry" in run.log

    def test_does_not_reconnect_an_already_connected_registry(self, tmp_path: Path) -> None:
        run = _run(tmp_path, "--cluster", "proef", STUB_REG_NETWORK='{"NetworkID":"abc"}')

        assert "docker network connect" not in run.log

    def test_documents_the_registry_in_kube_public(self, tmp_path: Path) -> None:
        run = _run(tmp_path, "--cluster", "proef")

        assert "kubectl --context kind-proef apply -f -" in run.log
        manifest = yaml.safe_load(run.kubectl_stdin)
        assert manifest["metadata"]["name"] == "local-registry-hosting"
        assert manifest["metadata"]["namespace"] == "kube-public"
        assert 'host: "localhost:5001"' in manifest["data"]["localRegistryHosting.v1"]

    def test_cluster_flag_selects_the_cluster(self, tmp_path: Path) -> None:
        run = _run(tmp_path, "--cluster=anders")

        assert "kind get nodes --name anders" in run.log
        assert "kubectl --context kind-anders" in run.log

    def test_cluster_defaults_to_the_sandbox(self, tmp_path: Path) -> None:
        run = _run(tmp_path)

        assert "kind get nodes --name rig-sandbox" in run.log

    def test_cluster_name_comes_from_the_environment(self, tmp_path: Path) -> None:
        run = _run(tmp_path, KIND_CLUSTER_NAME="uit-de-omgeving")

        assert "kind get nodes --name uit-de-omgeving" in run.log
        assert "kubectl --context kind-uit-de-omgeving" in run.log

    def test_cluster_flag_beats_the_environment(self, tmp_path: Path) -> None:
        """De kop belooft dat --cluster voorgaat; sandbox:setup geeft hem altijd mee."""
        run = _run(tmp_path, "--cluster", "van-de-vlag", KIND_CLUSTER_NAME="uit-de-omgeving")

        assert "kind get nodes --name van-de-vlag" in run.log
        assert "uit-de-omgeving" not in run.log

    def test_cluster_flag_without_a_name_is_refused(self, tmp_path: Path) -> None:
        """Zonder deze weigering zou --cluster als laatste woord het cluster leegmaken."""
        run = _run(tmp_path, "--cluster")

        assert run.returncode == 2
        assert "docker run" not in run.log
        assert "kind get nodes" not in run.log

    def test_registry_name_is_configurable(self, tmp_path: Path) -> None:
        """De naam draagt ook de hostnaam in hosts.toml; een vaste waarde daar wijst mis."""
        run = _run(tmp_path, "--cluster", "proef", KIND_REGISTRY_NAME="proef-registry")

        assert "--name proef-registry registry:2" in run.log
        assert run.hosts_toml.strip() == '[host."http://proef-registry:5000"]'
        assert "docker network connect kind proef-registry" in run.log

    def test_registry_image_is_configurable(self, tmp_path: Path) -> None:
        run = _run(tmp_path, "--cluster", "proef", KIND_REGISTRY_IMAGE="registry:3")

        assert "--name kind-registry registry:3" in run.log

    def test_port_is_configurable(self, tmp_path: Path) -> None:
        """De poort komt op drie plekken terug; ze moeten meebewegen."""
        run = _run(tmp_path, "--cluster", "proef", KIND_REGISTRY_PORT="5002")

        assert "-p 127.0.0.1:5002:5000" in run.log
        assert "/etc/containerd/certs.d/localhost:5002/hosts.toml" in run.log
        assert 'host: "localhost:5002"' in run.kubectl_stdin

    def test_default_port_avoids_the_dashboard_on_the_dev_server(self, tmp_path: Path) -> None:
        """claude-dashboard bindt op 127.0.0.1:5000; daar loopt `docker run` op stuk."""
        run = _run(tmp_path, "--cluster", "proef")

        assert "127.0.0.1:5000:5000" not in run.log

    def test_unknown_option_is_refused(self, tmp_path: Path) -> None:
        run = _run(tmp_path, "--kluster", "proef")

        assert run.returncode == 2
        assert "docker run" not in run.log

    def test_stops_when_the_registry_cannot_start(self, tmp_path: Path) -> None:
        """Doorgaan zou een cluster opleveren dat naar een registry wijst die er niet is."""
        run = _run(tmp_path, "--cluster", "proef", STUB_RUN_FAILS="1")

        assert run.returncode != 0
        assert run.hosts_toml == ""
        assert "kubectl" not in run.log

    def test_registry_is_configured_before_the_configmap(self, tmp_path: Path) -> None:
        """De nodes eerst, dan pas melden dat de registry er is."""
        calls = _run(tmp_path, "--cluster", "proef").calls()
        hosts_index = next(i for i, c in enumerate(calls) if "hosts.toml" in c)
        kubectl_index = next(i for i, c in enumerate(calls) if c.startswith("kubectl"))

        assert hosts_index < kubectl_index


class TestKindConfig:
    def test_containerd_reads_the_certs_directory(self, kind_config: dict) -> None:
        patches = "\n".join(kind_config["containerdConfigPatches"])

        assert '[plugins."io.containerd.grpc.v1.cri".registry]' in patches
        assert 'config_path = "/etc/containerd/certs.d"' in patches

    def test_config_survives_envsubst(self) -> None:
        """Het bestand gaat door envsubst; een $ in de patch zou daar leeglopen."""
        patches = yaml.safe_load(KIND_CONFIG.read_text())["containerdConfigPatches"]

        assert "$" not in "\n".join(patches)

    def test_the_path_matches_what_the_script_writes(self, kind_config: dict) -> None:
        """Wijzen ze naar verschillende mappen, dan schrijft het script in het niets."""
        patches = "\n".join(kind_config["containerdConfigPatches"])

        assert "/etc/containerd/certs.d" in SCRIPT.read_text()
        assert "/etc/containerd/certs.d" in patches


class TestDocumentation:
    """De weigering en de commentaren verwijzen naar de doc in plaats van zelf uit te leggen.

    Hernoemt of verplaatst iemand die, dan wijst de foutmelding op het cluster naar niets.
    """

    REFERRING_FILES = (SCRIPT, KIND_CONFIG, DOC, REPO_ROOT / "docs" / "sandbox-image-deploy-via-registry.md")

    @staticmethod
    def _referenced_paths(text: str) -> set[str]:
        without_urls = re.sub(r"https?://\S+", "", text)
        return {
            match.rstrip(".,;:`)") for match in re.findall(r"(?:docs|scripts|sandboxed-local)/[\w./-]+", without_urls)
        }

    @pytest.mark.parametrize("source", REFERRING_FILES, ids=lambda path: path.name)
    def test_every_referenced_repo_path_exists(self, source: Path) -> None:
        missing = [ref for ref in self._referenced_paths(source.read_text()) if not (REPO_ROOT / ref).exists()]

        assert not missing, f"{source.name} verwijst naar {missing}"

    @pytest.mark.usefixtures("bash_available")
    def test_the_refusal_points_at_a_document_that_exists(self, tmp_path: Path) -> None:
        run = _run(tmp_path, "--cluster", "proef", STUB_CONFIG_PATH="no")

        referenced = self._referenced_paths(run.stderr)
        assert referenced, run.stderr
        for ref in referenced:
            assert (REPO_ROOT / ref).exists(), f"de weigering wijst naar {ref}"


class TestTaskfile:
    def test_registry_task_calls_the_script(self, taskfile: dict) -> None:
        cmds = yaml.safe_dump(taskfile["tasks"][REGISTRY_TASK]["cmds"], width=10**6)

        assert "scripts/setup-kind-registry.sh" in cmds
        assert "--cluster rig-sandbox" in cmds

    def test_setup_configures_the_registry_after_creating_the_cluster(self, taskfile: dict) -> None:
        cmds = taskfile["tasks"]["sandbox:setup"]["cmds"]

        assert {"task": REGISTRY_TASK} in cmds
        assert cmds.index({"task": REGISTRY_TASK}) > cmds.index({"task": CREATE_CLUSTER_TASK})

    def test_setup_configures_the_registry_before_anything_needs_an_image(self, taskfile: dict) -> None:
        """Alles wat een image gebruikt komt erna, anders is de registry er nog niet."""
        cmds = taskfile["tasks"]["sandbox:setup"]["cmds"]
        registry_index = cmds.index({"task": REGISTRY_TASK})

        for later in ("sandbox:build-local-images-if-dev", "install-ingress-nginx", "sandbox:sync"):
            assert cmds.index({"task": later}) > registry_index
