"""Guard: de platformsleutel komt niet opnieuw bij de projectsleutels (RC-334).

De projectsleutels gaan sinds RC-334 door een eigen naad, ``wrap_project_key`` /
``unwrap_project_key`` in ``opi/utils/age.py``, en worden ingepakt met de project-KEK die
alleen in de OPI-pod staat. Pakt een volgende wijziging een projectsleutel weer rechtstreeks
met ``settings.SOPS_AGE_*`` in of uit, dan is de scheiding stil terug naar een sleutel voor
twee doelen, zonder dat een poort rood wordt. Deze guard legt elke verwijzing naar
``SOPS_AGE_PUBLIC_KEY``, ``SOPS_AGE_PRIVATE_KEY`` en ``get_global_private_key`` in ``opi/``
en in beide ``scripts/``-bomen tegen de lijst hieronder, met de gekwalificeerde functienaam als
sleutel in plaats van het regelnummer, zodat de lijst een wijziging eromheen overleeft.

De platformsleutel hoort op de plekken in ``TOEGESTAAN``: de settings zelf,
platformcredentials, platformsecrets en de twee functies die de keuze tussen KEK en
platformsleutel maken. Hoort een nieuwe plek daar echt bij, zet hem erin met zijn reden.
"""

from __future__ import annotations

import ast
from pathlib import Path

PYTHON_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = PYTHON_ROOT.parents[1]

# De bomen die de guard afloopt, met de basis waartegen het pad in TOEGESTAAN staat. De
# repo-wortel heeft een eigen scripts/ naast die van het python-pakket.
BOMEN: tuple[tuple[Path, Path], ...] = (
    (PYTHON_ROOT / "opi", PYTHON_ROOT),
    (PYTHON_ROOT / "scripts", PYTHON_ROOT),
    (REPO_ROOT / "scripts", REPO_ROOT),
)

PLATFORMSLEUTEL_NAMEN = frozenset({"SOPS_AGE_PUBLIC_KEY", "SOPS_AGE_PRIVATE_KEY", "get_global_private_key"})

# (bestand, gekwalificeerde functienaam) -> waarom de platformsleutel hier hoort
TOEGESTAAN: dict[tuple[str, str], str] = {
    ("opi/core/config.py", "_get_settings"): "de settings zelf: hier wordt de platformsleutel geladen",
    ("opi/utils/age.py", "get_global_private_key"): "de accessor van de platformsleutel",
    ("opi/utils/age.py", "decrypt_password_smart_auto"): "platformcredentials zoals REGISTRY_PASSWORD",
    ("opi/utils/age.py", "decrypt_password_smart_auto_sync"): "platformcredentials, sync kant",
    ("opi/utils/age.py", "project_key_candidates"): "de naad: de platformsleutel als terugval bij uitpakken",
    ("opi/utils/age.py", "project_wrapping_public_key"): "de naad: de platformsleutel zolang er geen KEK is",
    ("opi/utils/sops.py", "get_sops_private_key"): "platformsecrets: de SOPS-bestanden van het platform",
    ("opi/utils/sops.py", "decrypt_sops_file"): "platformsecrets: sops krijgt de sleutel per aanroep mee",
    (
        "opi/api/router.py",
        "_encrypt_for_task",
    ): "een geheim uit het verzoek in async_tasks.payload, geen projectsleutel",
    (
        "opi/utils/project_utils.py",
        "generate_self_service_project_yaml",
    ): "repositories[].password blijft bewust op de platformsleutel",
    ("opi/manager/argo_manager.py", "ArgoManager.prepare_repository_variables"): "repositories[].password",
    (
        "opi/manager/argo_manager.py",
        "ArgoManager.create_repository_secrets",
    ): "de ArgoCD repository-secrets zijn platformsecrets",
    (
        "opi/manager/argo_manager.py",
        "ArgoManager.create_infrastructure_application._infrastructuur",
    ): "de ArgoCD repository-secrets zijn platformsecrets",
    (
        "opi/forms/editables/converters.py",
        "AGEEncryptConverter._get_public_key",
    ): "de generieke AGEEncryptConverter, geen projectsleutel",
    (
        "opi/forms/editables/converters.py",
        "AGEEncryptConverter.read",
    ): "de generieke AGEEncryptConverter, geen projectsleutel",
    (
        "opi/services/user_otp_service.py",
        "UserOtpService.start_registration",
    ): "het OTP-zaad van een gebruiker is een gebruikersgeheim, geen projectsleutel",
    (
        "opi/services/user_otp_service.py",
        "UserOtpService.resume_registration",
    ): "het OTP-zaad van een gebruiker is een gebruikersgeheim, geen projectsleutel",
    (
        "opi/services/user_otp_service.py",
        "UserOtpService.confirm_registration",
    ): "het OTP-zaad van een gebruiker is een gebruikersgeheim, geen projectsleutel",
    (
        "opi/services/user_otp_service.py",
        "UserOtpService.verify",
    ): "het OTP-zaad van een gebruiker is een gebruikersgeheim, geen projectsleutel",
    (
        "opi/forms/editables/template.py",
        "load_project_template",
    ): "repositories[].password blijft bewust op de platformsleutel",
}


def _omsluitende_functie(node: ast.AST, ouders: dict[ast.AST, ast.AST]) -> str:
    """De naam van de functie om een treffer, met de klasse erbij.

    Zonder de klasse zou een plek als converters ``read`` elke ``read()`` in dat bestand
    toestaan, en dat zijn er 26.
    """
    delen: list[str] = []
    while node in ouders:
        node = ouders[node]
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            delen.append(node.name)
    return ".".join(reversed(delen)) or "<module>"


def _treffers_in(path: Path) -> list[tuple[str, int, str]]:
    """De verwijzingen naar de platformsleutel in dit bestand, als (functie, regel, naam)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    ouders = {kind: node for node in ast.walk(tree) for kind in ast.iter_child_nodes(node)}

    treffers: list[tuple[str, int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in PLATFORMSLEUTEL_NAMEN:
            naam = node.attr
        elif isinstance(node, ast.Name) and node.id in PLATFORMSLEUTEL_NAMEN and isinstance(node.ctx, ast.Load):
            # Store-context is de definitie van de setting zelf, geen gebruik.
            naam = node.id
        else:
            continue
        treffers.append((_omsluitende_functie(node, ouders), node.lineno, naam))
    return treffers


def _alle_treffers() -> list[tuple[str, str, int, str]]:
    alles: list[tuple[str, str, int, str]] = []
    for boom, basis in BOMEN:
        for path in sorted(boom.rglob("*.py")):
            relatief = path.relative_to(basis).as_posix()
            alles.extend((relatief, functie, regel, naam) for functie, regel, naam in _treffers_in(path))
    return alles


def test_de_platformsleutel_staat_alleen_op_de_toegestane_plekken() -> None:
    buiten_de_lijst = [
        f"{bestand}:{regel} in {functie}() gebruikt {naam}"
        for bestand, functie, regel, naam in _alle_treffers()
        if (bestand, functie) not in TOEGESTAAN
    ]

    assert not buiten_de_lijst, (
        "De platformsleutel wordt gebruikt buiten de toegestane plekken. Gaat het om een "
        "projectsleutel, gebruik wrap_project_key/unwrap_project_key uit opi/utils/age.py. "
        "Hoort de plek er echt bij, zet hem met zijn reden in TOEGESTAAN:\n" + "\n".join(buiten_de_lijst)
    )


def test_elke_plek_in_de_lijst_bestaat_nog() -> None:
    """Een plek die verdween moet uit de lijst, anders dekt die iets wat er niet meer is."""
    gevonden = {(bestand, functie) for bestand, functie, _, _ in _alle_treffers()}

    assert not set(TOEGESTAAN) - gevonden


def test_de_guard_vuurt_op_een_projectsleutel_onder_de_platformsleutel(tmp_path: Path) -> None:
    """De vorm die RC-334 juist wegnam, moet de guard rood maken."""
    probe = tmp_path / "probe.py"
    probe.write_text(
        "async def save_project(project):\n"
        "    project['config']['age-private-key'] = await encrypt_age_content(\n"
        "        key, settings.SOPS_AGE_PUBLIC_KEY\n"
        "    )\n",
        encoding="utf-8",
    )

    treffers = _treffers_in(probe)

    assert [(functie, naam) for functie, _, naam in treffers] == [("save_project", "SOPS_AGE_PUBLIC_KEY")]
    assert ("probe.py", "save_project") not in TOEGESTAAN
