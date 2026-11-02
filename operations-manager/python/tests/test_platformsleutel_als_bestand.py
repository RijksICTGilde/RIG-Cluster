"""RC-279: de platformsleutel komt als bestand binnen en kindprocessen erven geen geheimen.

De omgeving van een kindproces is niet te zien in een gewone test van een connector: die
mockt het subprocess weg. Daarom toetst dit bestand de twee plekken waar het vastligt, de
laadfunctie en `child_env`, plus het manifest dat ze voedt.
"""

from __future__ import annotations

import ast
import os
import pathlib

import pytest
import yaml
from opi.core.config import _get_settings, load_sops_age_key_content, parse_sops_age_key_content
from opi.utils.subprocess_env import PLATFORM_SECRET_NAMES, PLATFORM_SECRET_PREFIXES, child_env

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
KUSTOMIZE_ROOT = REPO_ROOT / "bootstrap/rig-system/kustomize/operations-manager"
BASE_DEPLOYMENT = KUSTOMIZE_ROOT / "base/deployment.yaml"
# Elke overlay en elke patch, niet een vaste lijst: een nieuwe overlay en een patch met een
# andere naam (enable-debug.yaml zet ook env) moeten meteen onder de toets vallen.
OVERLAY_PATCHES = sorted(KUSTOMIZE_ROOT.glob("overlays/*/patches/*.yaml"))
KUSTOMIZATIONS = sorted(KUSTOMIZE_ROOT.glob("overlays/*/kustomization.yaml"))
# De manifesten achter de secretRef-namen in envFrom. Een envFrom die hier niet staat laat
# de toets falen, zodat een nieuwe env-bron niet ongezien zijn geheimen laat erven.
ENV_SOURCE_MANIFESTS = {
    "operations-manager-access": KUSTOMIZE_ROOT / "overlays/odcn-production/operations-manager-access-secrets.yaml",
    "operations-manager-env-secrets": KUSTOMIZE_ROOT / "overlays/odcn-production/operations-manager-env-secrets.yaml",
}
# Dit secret staat niet in git: OPI schrijft het zelf bij het opstarten
# (opi/bootstrap/keycloak_setup.py, _update_operations_secret), met deze sleutels.
OPI_WRITTEN_ENV_SOURCES = {
    "operations-manager-keycloak": {
        "OIDC_CLIENT_ID",
        "OIDC_CLIENT_SECRET",
        "OIDC_DISCOVERY_URL",
        "OIDC_URL",
        "OIDC_REALM",
    },
}

# De aanroepen die een proces starten, per module. `asyncio.run` start geen proces en
# staat er daarom niet bij.
SUBPROCESS_STARTS: dict[str, tuple[str, ...]] = {
    "subprocess": ("run", "Popen", "call", "check_call", "check_output"),
    "asyncio": ("create_subprocess_exec", "create_subprocess_shell"),
}

KEY_CONTENT = (
    "# created: 2026-10-16T00:00:00Z\n"
    "# public key: age1testtesttesttesttesttesttesttesttesttesttesttesttestq7yq0q\n"
    "AGE-SECRET-KEY-1TESTTESTTESTTESTTESTTESTTESTTESTTESTTESTTESTTESTTESTTEST\n"
)


def _opi_containers(manifest: pathlib.Path) -> list[dict]:
    """De operations-manager-container uit een Deployment of een Deployment-patch."""
    doc = yaml.safe_load(manifest.read_text())
    if doc.get("kind") != "Deployment":
        return []
    containers = doc["spec"]["template"]["spec"]["containers"]
    return [c for c in containers if c["name"] == "operations-manager"]


def _opi_container(manifest: pathlib.Path) -> dict:
    return _opi_containers(manifest)[0]


def test_bestand_levert_beide_sleutels(tmp_path: pathlib.Path) -> None:
    key_file = tmp_path / "key"
    key_file.write_text(KEY_CONTENT)

    content = load_sops_age_key_content(str(key_file), None)

    public_key, private_key = parse_sops_age_key_content(content or "")
    assert public_key is not None
    assert public_key.startswith("age1")
    assert private_key is not None
    assert private_key.startswith("AGE-SECRET-KEY-")


def test_leeg_bestand_is_een_opstartfout(tmp_path: pathlib.Path) -> None:
    key_file = tmp_path / "key"
    key_file.write_text("   \n")

    with pytest.raises(RuntimeError, match=str(key_file)):
        load_sops_age_key_content(str(key_file), None)


def test_afwezig_bestand_is_een_opstartfout(tmp_path: pathlib.Path) -> None:
    key_file = tmp_path / "er-staat-niets"

    with pytest.raises(RuntimeError, match=str(key_file)):
        load_sops_age_key_content(str(key_file), None)


def test_bestand_wint_niet_stil_van_de_terugval(tmp_path: pathlib.Path) -> None:
    """Een gezet maar kapot pad valt niet terug op de env, ook niet als die gevuld is."""
    key_file = tmp_path / "er-staat-niets"

    with pytest.raises(RuntimeError, match=str(key_file)):
        load_sops_age_key_content(str(key_file), KEY_CONTENT)


def test_terugval_op_de_omgeving_werkt_nog() -> None:
    assert load_sops_age_key_content(None, KEY_CONTENT) == KEY_CONTENT
    assert load_sops_age_key_content(None, None) is None


def test_env_terugval_blijft_niet_in_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOPS_AGE_KEY_CONTENT", KEY_CONTENT)
    monkeypatch.delenv("SOPS_AGE_KEY_FILE", raising=False)
    monkeypatch.delenv("SOPS_AGE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("SOPS_AGE_PRIVATE_KEY", raising=False)

    settings = _get_settings()

    assert settings.SOPS_AGE_PUBLIC_KEY is not None
    assert settings.SOPS_AGE_PRIVATE_KEY is not None
    assert "SOPS_AGE_KEY_CONTENT" not in os.environ


def test_child_env_weert_de_platformgeheimen(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in [*PLATFORM_SECRET_NAMES, "SOPS_AGE_KEY_CONTENT", "SOPS_AGE_PRIVATE_KEY"]:
        monkeypatch.setenv(name, "geheim")
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("HOME", "/home/opi")

    env = child_env()

    assert not [name for name in env if name in PLATFORM_SECRET_NAMES]
    assert not [name for name in env if name.startswith(PLATFORM_SECRET_PREFIXES)]
    assert env["PATH"] == "/usr/bin"
    assert env["HOME"] == "/home/opi"


def test_child_env_weert_ook_de_project_kek(monkeypatch: pytest.MonkeyPatch) -> None:
    """De sleutel die elk projectgeheim opent gaat net zo goed niet mee als de platformsleutel."""
    for name in ["PROJECT_KEK_FILE", "PROJECT_KEK_PUBLIC_KEY", "PROJECT_KEK_PRIVATE_KEY"]:
        monkeypatch.setenv(name, "geheim")

    env = child_env()

    assert not [name for name in env if name.startswith("PROJECT_KEK_")]


def test_child_env_voegt_extra_toe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOPS_AGE_PRIVATE_KEY", "van het platform")

    env = child_env({"SOPS_AGE_KEY": "van dit project", "PGPASSWORD": "van deze database"})

    assert env["SOPS_AGE_KEY"] == "van dit project"
    assert env["PGPASSWORD"] == "van deze database"


def _namen_uit_secret(naam: str, bron: pathlib.Path) -> set[str]:
    if naam in OPI_WRITTEN_ENV_SOURCES:
        return OPI_WRITTEN_ENV_SOURCES[naam]
    assert naam in ENV_SOURCE_MANIFESTS, f"onbekende envFrom-bron {naam} in {bron}"
    secret = yaml.safe_load(ENV_SOURCE_MANIFESTS[naam].read_text())
    return set(secret.get("data") or secret["stringData"])


def _inline_deployment_patches() -> list[tuple[pathlib.Path, list[dict]]]:
    """De JSON-patches die een kustomization zelf op het Deployment zet, zonder patchbestand."""
    kustomizations = {pad: yaml.safe_load(pad.read_text()) for pad in KUSTOMIZATIONS}
    return [
        (pad, yaml.safe_load(entry["patch"]))
        for pad, doc in kustomizations.items()
        for entry in doc.get("patches") or []
        if "patch" in entry and (entry.get("target") or {}).get("kind") == "Deployment"
    ]


def _namen_uit_env_bronnen() -> set[str]:
    """Elke naam die het deployment uit een secret in de omgeving zet."""
    namen: set[str] = set()
    for manifest in [BASE_DEPLOYMENT, *OVERLAY_PATCHES]:
        for container in _opi_containers(manifest):
            for entry in container.get("env") or []:
                if "secretKeyRef" in (entry.get("valueFrom") or {}):
                    namen.add(entry["name"])
            for bron in container.get("envFrom") or []:
                namen |= _namen_uit_secret((bron.get("secretRef") or {})["name"], manifest)
    for kustomization, operaties in _inline_deployment_patches():
        for operatie in operaties:
            pad = operatie.get("path", "")
            waarde = operatie.get("value") or {}
            if "/envFrom" in pad:
                namen |= _namen_uit_secret((waarde.get("secretRef") or {})["name"], kustomization)
            elif "/env" in pad and "secretKeyRef" in (waarde.get("valueFrom") or {}):
                namen.add(waarde["name"])
    return namen


# Namen die de lijst bewust draagt zonder dat een manifest ze zet. Geen manifest betekent
# hier niet "kan niet voorkomen": de sandbox maakt zijn env-secret uit een `.env` die deze
# repo niet kent. Leeg sinds RC-345: ARGOCD_PASSWORD staat weer in de sandbox-overlay.
ZONDER_MANIFEST: frozenset[str] = frozenset()


def test_lijst_dekt_elk_geheim_uit_de_manifesten() -> None:
    """Een nieuw geheim in een env-bron of in een secretKeyRef erft niet stil weer mee."""
    uit_manifesten = _namen_uit_env_bronnen()

    ongedekt = {name for name in uit_manifesten if name not in PLATFORM_SECRET_NAMES}
    assert not {name for name in ongedekt if not name.startswith(PLATFORM_SECRET_PREFIXES)}
    assert not PLATFORM_SECRET_NAMES - uit_manifesten - ZONDER_MANIFEST


def test_deployment_mount_de_sleutel_in_plaats_van_hem_in_de_omgeving_te_zetten() -> None:
    container = _opi_container(BASE_DEPLOYMENT)
    env = {entry["name"]: entry for entry in container["env"]}

    assert "SOPS_AGE_KEY_CONTENT" not in env
    assert env["SOPS_AGE_KEY_FILE"]["value"] == "/var/run/secrets/zad/sops-age/key"

    mount = next(m for m in container["volumeMounts"] if m["name"] == "sops-age-key")
    assert mount["mountPath"] == "/var/run/secrets/zad/sops-age"
    assert mount["readOnly"] is True

    deployment = yaml.safe_load(BASE_DEPLOYMENT.read_text())
    volume = next(v for v in deployment["spec"]["template"]["spec"]["volumes"] if v["name"] == "sops-age-key")
    assert volume["secret"]["secretName"] == "sops-age-key"
    assert volume["secret"]["defaultMode"] == 0o440
    assert deployment["spec"]["template"]["spec"]["securityContext"]["fsGroup"] == 1001


def test_deployment_zet_de_platte_sops_bestanden_in_geheugen() -> None:
    container = _opi_container(BASE_DEPLOYMENT)
    env = {entry["name"]: entry for entry in container["env"]}
    pad = env["SECRET_TEMP_DIR"]["value"]

    mount = next(m for m in container["volumeMounts"] if m["name"] == "secret-tmp")
    assert mount["mountPath"] == pad

    deployment = yaml.safe_load(BASE_DEPLOYMENT.read_text())
    volume = next(v for v in deployment["spec"]["template"]["spec"]["volumes"] if v["name"] == "secret-tmp")
    assert volume["emptyDir"]["medium"] == "Memory"


def test_overlays_zetten_de_sleutel_niet_terug_in_de_omgeving() -> None:
    for manifest in OVERLAY_PATCHES:
        for container in _opi_containers(manifest):
            env = {entry["name"] for entry in container.get("env") or []}
            assert "SOPS_AGE_KEY_CONTENT" not in env, manifest
            assert "SOPS_AGE_KEY_FILE" not in env, manifest
            assert "SECRET_TEMP_DIR" not in env, manifest


async def test_sops_handler_schrijft_in_de_secret_temp_dir(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from opi.core.config import settings
    from opi.handlers.sops import SopsHandler

    secret_temp_dir = tmp_path / "secret-tmp"
    secret_temp_dir.mkdir()
    monkeypatch.setattr(settings, "SECRET_TEMP_DIR", str(secret_temp_dir))
    monkeypatch.setattr(settings, "TEMP_DIR", str(tmp_path / "schijf"))

    gebruikte_paden: list[str] = []

    class Resultaat:
        returncode = 0
        stdout = "versleuteld"
        stderr = ""

    async def nep_run_command(cmd: list[str], env: dict[str, str] | None = None) -> Resultaat:
        gebruikte_paden.append(cmd[-1])
        return Resultaat()

    handler = SopsHandler()
    monkeypatch.setattr(handler, "_run_command", nep_run_command)

    await handler.encrypt_content("geheim", "age1publiek")
    await handler.decrypt_content("geheim", "AGE-SECRET-KEY-1")

    assert gebruikte_paden
    assert all(pathlib.Path(pad).parent == secret_temp_dir for pad in gebruikte_paden)


def _mist_een_echte_env(knoop: ast.Call) -> bool:
    """`env=None` erft net als een ontbrekende `env=` de volle omgeving."""
    for sleutelwoord in knoop.keywords:
        if sleutelwoord.arg == "env":
            return isinstance(sleutelwoord.value, ast.Constant) and sleutelwoord.value.value is None
    return True


def _starts_zonder_env(bron: str) -> list[int]:
    """Regelnummers van procesaanroepen in `bron` die geen (werkende) `env=` meegeven."""
    regels: list[int] = []
    for knoop in ast.walk(ast.parse(bron)):
        if not isinstance(knoop, ast.Call) or not isinstance(knoop.func, ast.Attribute):
            continue
        module = knoop.func.value
        module_naam = module.id if isinstance(module, ast.Name) else getattr(module, "attr", "")
        if knoop.func.attr not in SUBPROCESS_STARTS.get(module_naam, ()):
            continue
        if _mist_een_echte_env(knoop):
            regels.append(knoop.lineno)
    return regels


def test_geen_enkel_kindproces_erft_de_volle_omgeving() -> None:
    """De drie spellingen waarmee `os.environ` ongefilterd een subprocess in ging.

    De derde is het ontbreken van `env=`: dan erft het kind de volle omgeving van OPI, en
    bij sops is dat de platformsleutel, want die leest `SOPS_AGE_KEY_FILE` zelf.
    """
    opi = REPO_ROOT / "operations-manager/python/opi"
    gevonden: list[str] = []
    for pad in opi.rglob("*.py"):
        bron = pad.read_text()
        nummers = [
            nummer
            for nummer, regel in enumerate(bron.splitlines(), start=1)
            if "os.environ.copy()" in regel or "{**os.environ" in regel
        ]
        nummers += _starts_zonder_env(bron)
        gevonden += [f"{pad.relative_to(opi)}:{nummer}" for nummer in sorted(nummers)]
    assert not gevonden


def test_de_guard_ziet_een_ontbrekende_env() -> None:
    """Zonder deze toets kan de guard hierboven stil langs elke spelling heen kijken."""
    assert _starts_zonder_env("subprocess.run(cmd, env=child_env())") == []
    assert _starts_zonder_env("subprocess.run(cmd)") == [1]
    assert _starts_zonder_env("subprocess.Popen(cmd)") == [1]
    assert _starts_zonder_env("await asyncio.create_subprocess_exec(*cmd)") == [1]
    assert _starts_zonder_env("await asyncio.create_subprocess_shell(cmd)") == [1]
    assert _starts_zonder_env("asyncio.run(main())") == []
    # env=None erft net als een ontbrekende env= de volle omgeving van het ouderproces.
    assert _starts_zonder_env("subprocess.run(cmd, env=None)") == [1]
