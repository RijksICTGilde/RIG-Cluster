"""Elke key in een ArgoCD-CR moet aankomen bij het component dat hem moet lezen (RC-250).

Zeven van de tien keys in het ``extraConfig``-blok op odcn-production kwamen nergens aan.
De operator kopieert dat blok in **argocd-cm** (``controllers/argocd/configmap.go:519``, en
zijn eigen documentatie zegt het ook: "add dynamic entries to Argo CD configmap"), en een
cmd-params-key wordt daar door niemand gelezen. Hij hoort in ``argocd-cmd-params-cm``.

Negen maanden ongemerkt, omdat de vergissing met het blote oog onzichtbaar is: je moet per
key weten welke ConfigMap hem leest. Zes van de zeven veranderden bovendien niets, omdat ze
de operator-default herhaalden, bij een functie hoorden die uit staat, of langs de werkende
route in dezelfde CR al goed stonden. Daardoor leek het blok te werken: wat je zou nameten
werkte inderdaad.

Gemeten op sandboxed-local op 5 oktober 2026, waar hetzelfde blok stond:

* het command van ``argocd-application-controller-0`` was ``--operation-processors 10
  --status-processors 20 --kubectl-parallelism-limit 10``, de operator-defaults, en niet de
  30/60/20 uit ``extraConfig``;
* ``argocd-repo-server`` draaide zonder ``--parallelismlimit``;
* ``argocd-server`` draaide zonder ``--repo-server-timeout-seconds``;
* ``argocd-cmd-params-cm`` bestond in die namespace niet eens.

Deze guard keert de faalrichting om. Een key die hier niet in een van de twee lijsten
hieronder staat maakt de toets rood, dus een nieuwe key is rood tot iemand opschrijft welke
ConfigMap hem leest. Dat is wat "beter opletten" niet kan: de lijsten blijven met de hand
bijgehouden, maar het bewijs eromheen is nu een voorwaarde voor groen in plaats van iets wat
je achteraf had moeten doen.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_OVERLAYS_FROM_REPO_ROOT = "bootstrap/rig-system/kustomize/overlays"
_OVERLAYS = _REPO_ROOT / _OVERLAYS_FROM_REPO_ROOT

#: Keys die de operator in ``argocd-cm`` zet EN die ArgoCD daar leest, dus de enige die in
#: ``spec.extraConfig`` thuishoren. Alleen wat onze CR's gebruiken staat erin; een key
#: toevoegen vraagt het bewijs dat argocd-cm hem leest.
ARGOCD_CM_KEYS = {
    # De reconciliatie-intervallen. ``timeout.reconciliation`` is ook de enige argocd-cm-key
    # die de operator aan een env-variabele wiret (ARGOCD_RECONCILIATION_TIMEOUT), gemeten
    # op odcn-production en op sandboxed-local.
    "timeout.reconciliation",
    "timeout.hard.reconciliation",
}

#: ``resource.customizations.*`` is een familie en geen vaste key: de staart is
#: ``<group>_<Kind>``. Een prefix dus, geen opsomming per Kind.
ARGOCD_CM_KEY_PREFIXES = ("resource.customizations.",)

#: Keys die in ``argocd-cmd-params-cm`` horen. In ``extraConfig`` zijn ze dood. De acht
#: hieronder zijn precies die uit het blok dat deze taak heeft opgeruimd, zodat die
#: vergissing niet terug kan komen zonder een melding die zegt wat eraan scheelt.
CMD_PARAMS_KEYS = {
    "controller.sync.timeout.seconds",
    "controller.status.processors",
    "controller.operation.processors",
    "controller.repo.server.timeout.seconds",
    "controller.kubectl.parallelism.limit",
    "controller.sharding.algorithm",
    "reposerver.parallelism.limit",
    "server.repo.server.timeout.seconds",
}

#: De enige cmd-params-key die onder deze operator wel bij een component aankomt. Hij bouwt
#: zijn eigen StatefulSet en wiret daar precies twee ConfigMap-verwijzingen in:
#: ARGOCD_APPLICATION_CONTROLLER_PERSIST_RESOURCE_HEALTH naar deze key, en
#: ARGOCD_RECONCILIATION_TIMEOUT naar ``timeout.reconciliation`` in argocd-cm. De rest geeft
#: hij als getypeerde flag mee uit ``spec.controller.*``, dus ``spec.cmdParams`` is voor elke
#: andere key net zo dood als ``extraConfig``.
CMD_PARAMS_KEYS_REACHING_A_COMPONENT = {"controller.resource.health.persist"}

#: De grens op een sync-operation, als flag omdat geen cmd-params-key de controller bereikt.
#: 600s: ruim boven OPI's eigen wacht van 300s.
SYNC_TIMEOUT_ARG = "--sync-timeout=600"


def _argocd_crs() -> list[tuple[str, dict]]:
    """Elke ArgoCD-CR onder de rig-system-overlays, gevonden op ``kind`` en niet op bestandsnaam.

    Dezelfde vorm als de walk in ``test_argocd_handwritten_application_syncoptions.py``: een
    overlay of een CR die later wordt toegevoegd, hernoemd of verplaatst blijft zo gedekt in
    plaats van stil uit de parametrize te vallen.
    """
    found: list[tuple[str, dict]] = []
    for path in sorted(_OVERLAYS.rglob("*.yaml")):
        for document in yaml.safe_load_all(path.read_text()):
            if not document or document.get("kind") != "ArgoCD":
                continue
            found.append((str(path.relative_to(_REPO_ROOT)), document.get("spec", {}) or {}))
    return found


_CRS = _argocd_crs()
_IDS = [path for path, _ in _CRS]


def test_the_walk_finds_an_argocd_cr_in_every_overlay_and_reads_its_keys() -> None:
    """Een lege parametrize slaat over in plaats van te falen, dus zonder deze toets melden de
    checks hieronder succes over niets: dat is wat een verplaatste ``_OVERLAYS`` of een
    verkeerde ``parents``-index achterlaat. Dat de keys ook echt gelezen zijn staat er apart
    bij, want ``_argocd_crs`` valt bij een ontbrekende ``spec`` terug op een leeg dict en dan
    passeert elke check over nul keys."""
    covered = {Path(path).relative_to(_OVERLAYS_FROM_REPO_ROOT).parts[0] for path in _IDS}
    assert covered == {"odcn-production", "sandboxed-local"}, f"overlays gedekt: {sorted(covered)}"

    for path, spec in _CRS:
        assert spec.get("extraConfig"), f"{path} leverde geen extraConfig"
        assert "timeout.reconciliation" in spec["extraConfig"], f"{path} leverde {sorted(spec['extraConfig'])}"


@pytest.mark.parametrize(("path", "spec"), _CRS, ids=_IDS)
def test_no_cmd_params_key_sits_in_extra_config(path: str, spec: dict) -> None:
    """De vergissing zelf: een key die argocd-cmd-params-cm leest, gezet in argocd-cm."""
    misplaced = sorted(set(spec.get("extraConfig", {})) & CMD_PARAMS_KEYS)
    assert misplaced == [], (
        f"{path} zet {misplaced} in extraConfig; die key hoort in argocd-cmd-params-cm en "
        f"komt hier bij geen enkel component aan"
    )


@pytest.mark.parametrize(("path", "spec"), _CRS, ids=_IDS)
def test_every_extra_config_key_is_one_argocd_cm_reads(path: str, spec: dict) -> None:
    """De andere richting: een key die in geen van beide lijsten staat is onbewezen."""
    unknown = sorted(
        key
        for key in spec.get("extraConfig", {})
        if key not in ARGOCD_CM_KEYS and not key.startswith(ARGOCD_CM_KEY_PREFIXES)
    )
    assert unknown == [], (
        f"{path} zet {unknown} in extraConfig zonder dat is opgeschreven dat argocd-cm die "
        f"leest; zet hem in ARGOCD_CM_KEYS met het bewijs, of in CMD_PARAMS_KEYS"
    )


@pytest.mark.parametrize(("path", "spec"), _CRS, ids=_IDS)
def test_cmd_params_only_carries_a_key_that_reaches_a_component(path: str, spec: dict) -> None:
    """De valkuil bij het repareren: ``spec.cmdParams`` vult wel de juiste ConfigMap, maar deze
    operator wiret er op een na geen enkele key aan een component."""
    dead = sorted(set(spec.get("cmdParams", {})) - CMD_PARAMS_KEYS_REACHING_A_COMPONENT)
    assert dead == [], (
        f"{path} zet {dead} in cmdParams; deze operator geeft die niet aan een component "
        f"door, dus de key is daar net zo dood als in extraConfig"
    )


@pytest.mark.parametrize(("path", "spec"), _CRS, ids=_IDS)
def test_the_controller_has_an_upper_bound_on_a_sync(path: str, spec: dict) -> None:
    """Zonder deze flag is de default 0, dus nooit, en blokkeert een sync die op health wacht
    elke volgende sync op die Application. Het is de enige weg: als extraConfig-key komt hij
    niet aan, en als syncOption bestaat hij niet (RC-226)."""
    args = (spec.get("controller", {}) or {}).get("extraCommandArgs", []) or []
    assert SYNC_TIMEOUT_ARG in args, f"{path} draagt {args}"
