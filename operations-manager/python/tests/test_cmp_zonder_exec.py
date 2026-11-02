"""De CMP rendert zonder ``--enable-exec``, en meldt geen geheimen in een foutmelding.

Met ``--enable-exec`` voert kustomize elke exec-functie uit die een kustomization noemt,
en in een deploymentmap komt inhoud die niet van OPI is: vrije bestandsnamen met vrije
inhoud uit ``helmfile[].files``, en een integraal gekloonde chart-repo. Die vlag was er
alleen voor KSOPS, dus de plugin ontsleutelt die bestanden nu zelf en zet ze als resource
in de kustomization.

De tests draaien de functies uit de plugin zelf, zodat ze meeverhuizen als die verandert.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
from tests.cmp_plugin import render_script
from tests.programma import echt_programma

_SECRET_VALUE = "superspecifiekewaarde"

_KSOPS_GENERATOR = """\
apiVersion: viaduct.ai/v1
kind: ksops
metadata:
  name: secret-generator
  annotations:
    config.kubernetes.io/function: |
      exec:
        path: ksops
files: {files}
"""


def _kustomize() -> str:
    """Kustomize kent geen ``--version``, dus niet via ``echt_programma``."""
    found = shutil.which("kustomize")
    if found is None:
        pytest.skip("kustomize niet gevonden op PATH")
    return found


def _plugin_script() -> str:
    """Het bash-script dat beide plugins draaien."""
    return render_script()


def _plugin_functions() -> str:
    """De functies rond het inlijven van ksops, uit het pluginscript."""
    script = _plugin_script()
    match = re.search(
        r"^# Pad naar het kustomization-bestand van een map.*?^  inline_ksops_in_kustomization \"\$kust\" \"\$abs\"\n\}",
        script,
        re.DOTALL | re.MULTILINE,
    )
    assert match, "de ksops-functies staan niet meer waar de test ze zoekt"
    return match.group(0)


def _post_renderer_guard() -> str:
    """De functie die een gerenderde state met postRenderer weigert, uit het pluginscript."""
    script = _plugin_script()
    match = re.search(r"^reject_post_renderer_in_state\(\) \{\n.*?^\}\n", script, re.DOTALL | re.MULTILINE)
    assert match, "reject_post_renderer_in_state staat niet meer waar de test hem zoekt"
    return match.group(0)


def _post_renderer_runner() -> str:
    """De functie die de state rendert en de guard erop zet, uit het pluginscript."""
    script = _plugin_script()
    match = re.search(r"^reject_post_renderer\(\) \{\n.*?^\}\n", script, re.DOTALL | re.MULTILINE)
    assert match, "reject_post_renderer staat niet meer waar de test hem zoekt"
    return match.group(0)


def _guard_state(state: str, tmp_path: Path, age_key: str) -> subprocess.CompletedProcess[str]:
    """Draait de guard op een gerenderde state, zoals ``helmfile build`` hem geeft."""
    state_file = tmp_path / "state.yaml"
    state_file.write_bytes(state.encode())
    return _run(f'{_post_renderer_guard()}\nreject_post_renderer_in_state "$1"', tmp_path, age_key, str(state_file))


def _run(snippet: str, cwd: Path, age_key: str, *args: str) -> subprocess.CompletedProcess[str]:
    bash = echt_programma("bash")
    # yq en sops moeten voor de plugin op PATH staan, met hun echte pad en niet als shim.
    tools = Path(echt_programma("yq")).parent
    extra = Path(echt_programma("sops")).parent
    return subprocess.run(
        [bash, "-c", f"set -e\nset -o pipefail\n{_plugin_functions()}\n{snippet}", "_", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        env={
            "PATH": f"{tools}:{extra}:{Path(_kustomize()).parent}:/usr/bin:/bin",
            "SOPS_AGE_KEY": age_key,
            "HOME": str(cwd),
        },
    )


@pytest.fixture
def age_key(tmp_path: Path) -> str:
    """Een weggooisleutel, zodat de test geen clustersleutel nodig heeft."""
    keygen = echt_programma("age-keygen")
    key_file = tmp_path / "key.txt"
    subprocess.run([keygen, "-o", str(key_file)], capture_output=True, check=True)
    return next(line for line in key_file.read_text().splitlines() if line.startswith("AGE-SECRET-KEY-"))


def _public_key(age_key: str, tmp_path: Path) -> str:
    keygen = echt_programma("age-keygen")
    result = subprocess.run([keygen, "-y"], input=age_key, capture_output=True, text=True, check=True, cwd=tmp_path)
    return result.stdout.strip()


def _encrypt(plain: str, public_key: str, target: Path, tmp_path: Path) -> None:
    sops = echt_programma("sops")
    source = tmp_path / "plain.yaml"
    source.write_text(plain)
    result = subprocess.run(
        [sops, "--config", "/dev/null", "--encrypt", "--age", public_key, str(source)],
        capture_output=True,
        text=True,
        check=True,
    )
    target.write_text(result.stdout)


@pytest.fixture
def overlay(tmp_path: Path, age_key: str) -> Path:
    """Een overlay op een base met een ksops-generator, zoals de platform-overlays."""
    public_key = _public_key(age_key, tmp_path)

    base = tmp_path / "base"
    base.mkdir()
    (base / "kustomization.yaml").write_text(
        "apiVersion: kustomize.config.k8s.io/v1beta1\n"
        "kind: Kustomization\n"
        "generators:\n"
        "  - decrypt-sops.yaml\n"
        "resources:\n"
        "  - cm.yaml\n"
    )
    (base / "cm.yaml").write_text('apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: demo\ndata:\n  a: "1"\n')
    (base / "decrypt-sops.yaml").write_text(_KSOPS_GENERATOR.format(files="[geheim.sops.yaml]"))
    _encrypt(
        f"apiVersion: v1\nkind: Secret\nmetadata:\n  name: demo-secret\nstringData:\n  TOKEN: {_SECRET_VALUE}\n",
        public_key,
        base / "geheim.sops.yaml",
        tmp_path,
    )

    overlay = tmp_path / "overlay"
    overlay.mkdir()
    (overlay / "kustomization.yaml").write_text(
        "apiVersion: kustomize.config.k8s.io/v1beta1\n"
        "kind: Kustomization\n"
        "resources:\n"
        "  - ../base\n"
        "generators:\n"
        "  - decrypt-sops.yaml\n"
    )
    # Zoals OPI hem schrijft voor een deployment zonder geheimen.
    (overlay / "decrypt-sops.yaml").write_text(_KSOPS_GENERATOR.format(files="[]"))
    return overlay


def test_the_build_needs_no_exec_after_inlining(overlay: Path, age_key: str, tmp_path: Path) -> None:
    """De kern: na de stap staat er geen generator meer en bouwt kustomize zonder vlaggen."""
    result = _run('inline_ksops_generators "$1"', tmp_path, age_key, "overlay")
    assert result.returncode == 0, result.stderr

    base_kustomization = (tmp_path / "base/kustomization.yaml").read_text()
    assert "generators" not in base_kustomization
    assert "zz-ksops-1.yaml" in base_kustomization
    # Een generator met een lege files-lijst levert niets, maar moet net zo goed weg.
    assert "generators" not in (overlay / "kustomization.yaml").read_text()

    kustomize = _kustomize()
    build = subprocess.run([kustomize, "build", str(overlay)], capture_output=True, text=True)
    assert build.returncode == 0, build.stderr
    assert _SECRET_VALUE in build.stdout
    assert "demo" in build.stdout


def test_a_ksops_generator_without_a_files_list_fails(overlay: Path, age_key: str, tmp_path: Path) -> None:
    """Zonder files-lijst is de generator onleesbaar geworden, en zouden zijn secrets stil
    uit de render vallen. Een lege lijst mag wel: die levert bij ksops zelf ook niets."""
    kapot = _KSOPS_GENERATOR.replace("files: {files}\n", "")
    (tmp_path / "base/decrypt-sops.yaml").write_text(kapot)

    result = _run('inline_ksops_generators "$1"', tmp_path, age_key, "overlay")

    assert result.returncode == 1
    assert "no files list" in result.stderr


def test_a_non_ksops_exec_generator_stays_and_then_fails(overlay: Path, age_key: str, tmp_path: Path) -> None:
    """Alleen ksops wordt ingelijfd. Elke andere exec-functie blijft staan en strandt op
    de build, in plaats van te draaien zoals met --enable-exec."""
    base = tmp_path / "base"
    (base / "eigen-functie.yaml").write_text(
        "apiVersion: example.com/v1\n"
        "kind: EigenGenerator\n"
        "metadata:\n"
        "  name: eigen\n"
        "  annotations:\n"
        '    config.kubernetes.io/function: "exec:\\n  path: ./eigen\\n"\n'
    )
    kustomization = base / "kustomization.yaml"
    kustomization.write_text(
        kustomization.read_text().replace("  - decrypt-sops.yaml\n", "  - decrypt-sops.yaml\n  - eigen-functie.yaml\n")
    )

    result = _run('inline_ksops_generators "$1"', tmp_path, age_key, "overlay")
    assert result.returncode == 0, result.stderr
    assert "eigen-functie.yaml" in kustomization.read_text()

    kustomize = _kustomize()
    build = subprocess.run([kustomize, "build", str(overlay)], capture_output=True, text=True)
    assert build.returncode != 0
    assert "external plugins disabled" in build.stderr


def test_a_generator_name_is_never_a_yq_expression(overlay: Path, age_key: str, tmp_path: Path) -> None:
    """De namen in ``.generators`` komen uit een kustomization en gaan via de omgeving naar
    yq, zodat een naam met een aanhalingsteken de expressie niet kan openbreken."""
    kustomization = tmp_path / "base/kustomization.yaml"
    vijandig = 'a") | .kind = "Gekaapt'
    kustomization.write_text(
        kustomization.read_text().replace("  - decrypt-sops.yaml\n", f"  - decrypt-sops.yaml\n  - '{vijandig}'\n")
    )

    result = _run('inline_ksops_generators "$1"', tmp_path, age_key, "overlay")

    assert result.returncode == 0, result.stderr
    geschreven = kustomization.read_text()
    assert "kind: Kustomization" in geschreven
    assert vijandig in geschreven


def test_a_broken_decrypted_file_is_reported_by_name_only(overlay: Path, age_key: str, tmp_path: Path) -> None:
    """Een fout IN een ontsleuteld bestand mag geen enkele waarde eruit teruggeven: de
    stderr van kustomize citeert de regel waarop hij struikelde, en die is geheim."""
    public_key = _public_key(age_key, tmp_path)
    # Geldige YAML, maar geen resource: kustomize struikelt op het ontsleutelde bestand.
    _encrypt(f"stringData:\n  TOKEN: {_SECRET_VALUE}\n", public_key, tmp_path / "base/geheim.sops.yaml", tmp_path)

    inlined = _run('inline_ksops_generators "$1"', tmp_path, age_key, "overlay")
    assert inlined.returncode == 0, inlined.stderr

    build = _run(
        "report() {\n"
        '  local folder="$1" kustomize_stderr kustomize_rc=0\n'
        '  kustomize_stderr=$(kustomize build "$folder" 2>&1 1>/dev/null) || kustomize_rc=$?\n'
        + _failure_branch()
        + "}\n"
        'report "$1"',
        tmp_path,
        age_key,
        "overlay",
    )

    assert "fout in ontsleuteld bestand zz-ksops-1.yaml" in build.stderr
    assert _SECRET_VALUE not in build.stderr
    # De stderr van kustomize gaat niet mee: die citeert het bestand waarop hij struikelde.
    assert "missing Resource metadata" not in build.stderr


def _failure_branch() -> str:
    """De tak uit process_folder die de fout van kustomize meldt, uit de plugin zelf."""
    script = _plugin_script()
    match = re.search(r'^  if \[ "\$kustomize_rc" -ne 0 \]; then\n.*?^  fi\n', script, re.DOTALL | re.MULTILINE)
    assert match, "de foutmelding staat niet meer waar de test hem zoekt"
    return match.group(0).replace('exit "$kustomize_rc"', "exit 0")


def test_the_plugin_turns_off_the_insecure_helmfile_features() -> None:
    """Drift guard over een taalgrens: zonder deze variabelen draait ``exec`` in een
    helmfile-template weer, en helmfile heeft geen vlag voor postRenderer."""
    script = _plugin_script()

    assert "export HELMFILE_DISABLE_INSECURE_FEATURES=true" in script
    assert "export HELMFILE_DISABLE_HOOKS=true" in script
    assert "reject_post_renderer" in script


@pytest.mark.parametrize(
    ("vorm", "state"),
    [
        ("gewone regel", "releases:\n  - name: demo\n    chart: ./chart\n    postRenderer: ./evil.sh\n"),
        ("gequote key", 'releases:\n  - name: demo\n    "postRenderer": ./evil.sh\n'),
        ("flow-stijl", "releases: [{name: demo, chart: ./chart, postRenderer: ./evil.sh}]\n"),
        ("alleen args", "releases:\n  - name: demo\n    postRendererArgs: [-c, id]\n"),
        ("tweede document", "releases:\n  - name: a\n---\nreleases:\n  - name: b\n    postRenderer: ./evil.sh\n"),
        ("diep genest", "environments:\n  default:\n    values:\n      - postRenderer: ./evil.sh\n"),
    ],
)
def test_every_shape_of_post_renderer_is_refused(vorm: str, state: str, tmp_path: Path, age_key: str) -> None:
    """postRenderer valt onder geen HELMFILE_DISABLE_*-vlag, dus de plugin weigert zelf.

    Een gequote key, flow-stijl en een key die een template-expressie samenstelt zijn na
    het parsen dezelfde key: de guard weegt daarom de gerenderde state en niet de
    regelvorm. De template-vorm zelf verdwijnt in ``helmfile build``, en die stap heeft
    een echte helmfile nodig; ``test_the_guard_weighs_the_rendered_state`` houdt vast dat
    de plugin de guard op die uitvoer zet.
    """
    result = _guard_state(state, tmp_path, age_key)

    assert result.returncode == 1, result.stdout
    assert "postRenderer" in result.stderr


def test_an_unparsable_state_is_refused(tmp_path: Path, age_key: str) -> None:
    """Een NUL-byte laat ``grep -I`` het hele bestand overslaan. yq weigert hem, en een
    state die de guard niet kan lezen is een weigering: anders faalt hij open."""
    result = _guard_state("releases:\n  - name: demo\x00\n    postRenderer: ./evil.sh\n", tmp_path, age_key)

    assert result.returncode == 1
    assert "not parsable" in result.stderr


def test_the_refusal_names_no_value_from_the_state(tmp_path: Path, age_key: str) -> None:
    """De state bevat de ontsleutelde state-values, dus de melding noemt alleen de key."""
    result = _guard_state(
        f"environments:\n  default:\n    values:\n      - geheim: {_SECRET_VALUE}\n"
        "releases:\n  - name: demo\n    postRenderer: ./evil.sh\n",
        tmp_path,
        age_key,
    )

    assert result.returncode == 1
    assert _SECRET_VALUE not in result.stderr + result.stdout


def test_a_helmfile_state_without_post_renderer_passes(tmp_path: Path, age_key: str) -> None:
    result = _guard_state("releases:\n  - name: demo\n    chart: ./chart\n", tmp_path, age_key)

    assert result.returncode == 0, result.stderr


def test_the_guard_weighs_the_rendered_state() -> None:
    """Drift guard: zonder ``helmfile build`` weegt de guard de bron, en dan glipt een key
    die een template-expressie samenstelt erlangs."""
    script = _plugin_script()

    assert 'helmfile --file "$helmfile_name" "$@" build' in script
    assert 'reject_post_renderer_in_state "$state_file"' in script
    # Gequote en als array: anders krijgt de guard alleen het eerste argument, en weegt hij
    # een state zonder de environment die de template-stap wel meekrijgt.
    assert 'reject_post_renderer "$helmfile_name" "${helmfile_args[@]}"' in script


def test_a_refused_state_leaves_no_decrypted_file_behind(tmp_path: Path) -> None:
    """De gerenderde state houdt de ontsleutelde state-values, en de sidecar rendert elke
    Application: een weigering mag dat bestand niet in zijn /tmp achterlaten."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "helmfile"
    shim.write_text("#!/bin/sh\nprintf 'releases:\\n  - name: demo\\n    postRenderer: ./evil.sh\\n'\n")
    shim.chmod(0o755)
    tmp_root = tmp_path / "tmp"
    tmp_root.mkdir()
    bash = echt_programma("bash")
    tools = Path(echt_programma("yq")).parent

    result = subprocess.run(
        [
            bash,
            "-c",
            f"set -e\nset -o pipefail\n{_post_renderer_guard()}\n{_post_renderer_runner()}\n"
            "reject_post_renderer helmfile.yaml",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={"PATH": f"{bin_dir}:{tools}:/usr/bin:/bin", "TMPDIR": str(tmp_root), "HOME": str(tmp_path)},
    )

    assert result.returncode == 1, result.stdout
    assert "postRenderer" in result.stderr
    assert list(tmp_root.iterdir()) == []


def test_no_kustomize_build_in_the_plugin_enables_exec() -> None:
    builds = [line for line in _plugin_script().splitlines() if "kustomize build" in line]

    assert builds, "de plugin bouwt niets meer"
    for line in builds:
        assert "--enable-exec" not in line, line
        assert "--enable-alpha-plugins" not in line, line
