"""Docker aanroepen vanuit een testfixture, luid falend.

Hier staat wat twee fixtures met een echte container delen: de ORM-tests met hun vaste
Postgres (``tests/conftest.py``) en de authorization-wall-tests met hun Keycloak plus
oauth2-proxy (``tests/integration/authwall_harness.py``). Alleen het generieke deel staat
hier: een aanroep en een inspectie. Klaar zijn is per container iets anders (``pg_isready``
tegen een HTTP-endpoint), en dat blijft bij de fixture die het weet.
"""

import subprocess


class DockerError(RuntimeError):
    """Docker deed niet wat er gevraagd werd. Luid falen, nooit stil."""


def docker(
    *args: str,
    check: bool = True,
    timeout: int = 60,
    fout: type[DockerError] = DockerError,
) -> subprocess.CompletedProcess[str]:
    """Eén docker-commando. ``fout`` laat een fixture zijn eigen fouttype houden."""
    try:
        klaar = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise fout("docker niet gevonden; deze tests hebben een echte container nodig. Start Docker.") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise fout(f"docker {' '.join(args)} mislukte: {exc}") from exc
    if check and klaar.returncode != 0:
        raise fout(f"docker {' '.join(args)} gaf {klaar.returncode}: {klaar.stderr.strip()}")
    return klaar


def container_state(naam: str, fout: type[DockerError] = DockerError) -> tuple[bool, str]:
    """(draait hij, op welk image). Bestaat hij niet, dan (False, "")."""
    klaar = docker("inspect", "--format", "{{.State.Running}} {{.Config.Image}}", naam, check=False, fout=fout)
    if klaar.returncode != 0:
        return False, ""
    draait, _, image = klaar.stdout.strip().partition(" ")
    return draait == "true", image
