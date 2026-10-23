"""OPI krijgt nooit `pods/exec` op zijn service-account, en execcet nooit in een pod.

Besluit van de opdrachtgever (22-10-2026) bij RC-331: wie het service-account van OPI
overneemt, komt met `pods/exec` in elke pod van het cluster, en dat is een veel ruimer
recht dan een extra binary in het OPI-image. Een extra binary is daarom de weg, en deze
grendel houdt de andere weg dicht.

Twee kanten, want een van de twee alleen laat de deur open: het RECHT in RBAC (wat de
apiserver toestaat) en het GEBRUIK in de code (wat OPI aanroept). Een Role die exec geeft
is ook zonder aanroeper een pad naar elke pod, en een aanroep zonder recht is een fout die
pas in productie valt.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]

#: De bomen met RBAC die dit platform zelf uitrolt: de bootstrap van rig-system, de
#: infrastructuurcomponenten, en de manifests die OPI per tenant rendert.
_RBAC_TREES = (
    _REPO_ROOT / "bootstrap",
    _REPO_ROOT / "infrastructure",
    _REPO_ROOT / "operations-manager/python/manifests",
)

#: Het service-account van de OPI-pod (`serviceAccountName` in de deployment) en de naam
#: van de app, zodat een binding onder een van beide namen gevonden wordt.
_OPI_SERVICE_ACCOUNTS = frozenset({"namespace-manager", "operations-manager"})

#: De subresources die toegang tot een draaiende pod geven. `pods/log` staat er niet bij:
#: dat leest OPI echt (deploy-status en de OOM-watcher).
_EXEC_RESOURCES = frozenset({"pods/exec", "pods/attach", "pods/portforward"})

#: Hoe een exec op een pod in Python-code eruitziet: het kubectl-subcommando als argument,
#: het pad in de apiserver, of de exec-stream van de kubernetes-client.
#: `asyncio.create_subprocess_exec` valt hier niet onder: dat is een lokaal proces in de
#: eigen pod, en dat is juist de weg die deze grendel beschermt.
_EXEC_IN_CODE = re.compile(r"kubectl\s+exec|[\"']exec[\"']|connect_get_namespaced_pod_exec|pods/exec")


def _documents(path: Path) -> list[dict]:
    """De YAML-documenten in een bestand; wat niet als YAML leest, levert niets op.

    De manifests van OPI zijn Jinja-sjablonen: `{{ ... }}` op een waardepositie leest nog
    als YAML, een `{# ... #}`- of `{% if %}`-regel niet. Zo'n bestand valt dus buiten deze
    controle. Vandaag rendert er maar een RBAC (`cmp-sops-key-reader.yaml.jinja`), en die
    geeft `get` op een Secret aan de repo-server van ArgoCD, niet exec aan OPI.
    """
    try:
        return [document for document in yaml.safe_load_all(path.read_text()) if isinstance(document, dict)]
    except yaml.YAMLError:
        return []


def _yaml_bestanden() -> list[Path]:
    return sorted(pad for boom in _RBAC_TREES for pad in boom.rglob("*.y*ml*") if pad.is_file())


def _rollen_met_exec() -> dict[str, list[str]]:
    """Per Role- en ClusterRole-naam de bestanden waarin die exec geeft."""
    gevonden: dict[str, list[str]] = {}
    for pad in _yaml_bestanden():
        for document in _documents(pad):
            if document.get("kind") not in ("Role", "ClusterRole"):
                continue
            resources = {
                resource
                for regel in document.get("rules") or []
                if isinstance(regel, dict)
                for resource in (regel.get("resources") or [])
            }
            if resources & _EXEC_RESOURCES or "*" in resources:
                naam = (document.get("metadata") or {}).get("name", "")
                gevonden.setdefault(naam, []).append(str(pad.relative_to(_REPO_ROOT)))
    return gevonden


def _rollen_van_opi() -> dict[str, list[str]]:
    """Per rolnaam de bindingen die hem aan een service-account van OPI geven."""
    gevonden: dict[str, list[str]] = {}
    for pad in _yaml_bestanden():
        for document in _documents(pad):
            if document.get("kind") not in ("RoleBinding", "ClusterRoleBinding"):
                continue
            namen = {
                (subject or {}).get("name")
                for subject in document.get("subjects") or []
                if (subject or {}).get("kind") == "ServiceAccount"
            }
            if namen & _OPI_SERVICE_ACCOUNTS:
                rol = (document.get("roleRef") or {}).get("name", "")
                gevonden.setdefault(rol, []).append(str(pad.relative_to(_REPO_ROOT)))
    return gevonden


def test_geen_exec_recht_op_het_service_account_van_opi() -> None:
    """Geen Role of ClusterRole die OPI exec, attach of port-forward op een pod geeft."""
    met_exec = _rollen_met_exec()
    van_opi = _rollen_van_opi()
    overlap = {
        rol: {"rol staat in": met_exec[rol], "gebonden aan OPI in": van_opi[rol]}
        for rol in met_exec.keys() & van_opi.keys()
    }
    assert not overlap, (
        f"het service-account van OPI krijgt exec op een pod: {overlap}. "
        "Een binary in het OPI-image is de weg, niet een exec-recht."
    )


def test_rollen_met_exec_slaat_een_niet_dict_regel_over(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Een `rules`-entry die geen dict is wordt overgeslagen, niet een AttributeError.

    De isinstance-controle stond ooit na het uitpakken van `resources`, dus crashte
    hierop in plaats van de entry te negeren.
    """
    (tmp_path / "role.yaml").write_text(
        "kind: Role\nmetadata:\n  name: met-rommelregel\nrules:\n  - resources: [pods/exec]\n  - niet-een-dict\n"
    )
    monkeypatch.setattr("test_opi_geen_pod_exec._RBAC_TREES", (tmp_path,))
    monkeypatch.setattr("test_opi_geen_pod_exec._REPO_ROOT", tmp_path)

    gevonden = _rollen_met_exec()

    assert gevonden == {"met-rommelregel": ["role.yaml"]}


def test_opi_execcet_niet_in_een_pod() -> None:
    """Geen aanroep in `opi/` die een commando in een andere pod draait."""
    opi = _REPO_ROOT / "operations-manager/python/opi"
    treffers = [
        f"{pad.relative_to(_REPO_ROOT)}:{nummer}: {regel.strip()}"
        for pad in sorted(opi.rglob("*.py"))
        for nummer, regel in enumerate(pad.read_text().splitlines(), start=1)
        if _EXEC_IN_CODE.search(regel)
    ]
    assert not treffers, (
        f"OPI draait een commando in een andere pod: {treffers}. "
        "Een subprocess in de eigen pod heeft dat recht niet nodig."
    )
