"""Een wachtwoord in een git-URL mag nooit in de log belanden.

``_obfuscate_git_command`` maskeerde alleen ``https://``. Een gitserver binnen het cluster
draait op plain http, want TLS voegt daar niets toe, en die URL ging daardoor ongemaskeerd
naar de DEBUG-log. Op fundament-poc stond het Forgejo-wachtwoord daarmee leesbaar in
``kubectl logs`` en in Loki:

    Running Git command: git clone ... http://rig-admin:<wachtwoord>@forgejo...:3000/...

Twee logregels omzeilden de functie bovendien helemaal en logden
``repo_url_with_path`` rechtstreeks; die bouwt de URL juist mét inloggegevens.

Rood (alleen https gedekt): de http-variant houdt zijn wachtwoord.
Groen: elk schema wordt gemaskeerd en URL's zonder inloggegevens blijven leesbaar.
"""

import pytest
from opi.connectors.git import _obfuscate_git_command

_WACHTWOORD = "CArczGkFfMyUB9nKCVYsipSw"


@pytest.mark.parametrize(
    "commando",
    [
        f"git clone http://rig-admin:{_WACHTWOORD}@forgejo.rig-system.svc.cluster.local:3000/rig-admin/zad-projects.git .",
        f"git ls-remote --symref http://rig-admin:{_WACHTWOORD}@forgejo.rig-system.svc.cluster.local:3000/x.git HEAD",
        f"git push https://git:{_WACHTWOORD}@github.com/org/repo.git main",
        f"git fetch ssh://git:{_WACHTWOORD}@example.com/org/repo.git",
    ],
)
def test_wachtwoord_verdwijnt_ongeacht_het_schema(commando: str) -> None:
    resultaat = _obfuscate_git_command(commando)
    assert _WACHTWOORD not in resultaat
    assert ":***@" in resultaat


def test_gebruikersnaam_en_host_blijven_leesbaar() -> None:
    """Maskeren mag het foutzoeken niet onmogelijk maken."""
    resultaat = _obfuscate_git_command(
        f"git clone http://rig-admin:{_WACHTWOORD}@forgejo.rig-system.svc.cluster.local:3000/rig-admin/zad-projects.git ."
    )
    assert "rig-admin:***@forgejo.rig-system.svc.cluster.local:3000" in resultaat


@pytest.mark.parametrize(
    "commando",
    [
        "git clone ssh://git@github.com/org/repo.git",
        "git clone https://github.com/RijksICTGilde/RIG-Cluster.git",
        "git status",
    ],
)
def test_zonder_inloggegevens_ongewijzigd(commando: str) -> None:
    assert _obfuscate_git_command(commando) == commando


def test_token_zonder_gebruikersnaam() -> None:
    resultaat = _obfuscate_git_command("git clone https://ghp_GEHEIMTOKEN123@github.com/org/repo.git")
    assert "ghp_GEHEIMTOKEN123" not in resultaat
    assert "https://***@github.com" in resultaat
