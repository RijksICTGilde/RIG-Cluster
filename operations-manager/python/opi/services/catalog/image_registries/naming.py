"""Naamregels die alleen deze dienst kent, zodat de map verplaatsbaar blijft."""

from __future__ import annotations

import hashlib

from opi.utils.naming import sanitize_kubernetes_name

#: De operator plakt dit achter de naam van het pull-secret dat hij maakt.
PULL_SECRET_POSTFIX = "robot-pull-secret"

UPSTREAM_HASH_LENGTH = 8


def upstream_host(upstream: str) -> str:
    """``code.overheid.nl/robbert`` -> ``code.overheid.nl``."""
    return upstream.split("/", 1)[0]


def friendly_name(upstream: str) -> str:
    """De ``friendlyName`` voor de Quay-organisatie: de host zonder TLD en zonder punten.

    Quay verbiedt een punt in dat veld. De TLD valt weg omdat de gedeelde organisaties op
    ODCN (``ghcr-rig``, ``code-overheid-rig``) diezelfde vorm hebben.
    """
    host = upstream_host(upstream).split(":", 1)[0]
    labels = host.split(".")
    stem = labels[:-1] if len(labels) > 1 else labels
    return sanitize_kubernetes_name("".join(stem))


def upstream_namespace(upstream: str) -> str:
    """Het pad achter de host, leeg als de upstream alleen een host is."""
    _, _, path = upstream.partition("/")
    return path.strip("/")


def upstream_hash(upstream: str) -> str:
    """Het onderscheidende deel van de suffix.

    Een hash en niet de leesbare namespace, want beide delen van de suffix gaan door
    ``sanitize_kubernetes_name`` en die maakt van ``.`` en ``/`` een ``-``: zonder een
    deel van vaste lengte aan het eind is de samenvoeging niet omkeerbaar en botsen
    project ``demo`` + ``ghcr.io/team`` en project ``demo-team`` + ``ghcr.io``. Over de
    HELE upstream, want ``friendly_name`` laat de TLD weg.
    """
    return hashlib.sha256(upstream.strip().strip("/").encode()).hexdigest()[:UPSTREAM_HASH_LENGTH]


def organization_suffix(upstream: str, project_name: str) -> str:
    """Wat wij als ``spec.suffix`` op de ``Organization`` zetten.

    De naam die de operator eromheen bouwt draagt alleen de host, dus zonder dit deel
    vallen ``ghcr.io/orga`` en ``ghcr.io/orgb`` van hetzelfde project op één organisatie.
    """
    return f"{sanitize_kubernetes_name(project_name)}-{upstream_hash(upstream)}"


def organization_name(upstream: str, customer_name: str, project_name: str) -> str:
    """``<friendlyName>-<customerName>-<suffix>``, de naam die de operator samenstelt."""
    return sanitize_kubernetes_name(
        f"{friendly_name(upstream)}-{customer_name}-{organization_suffix(upstream, project_name)}"
    )


def pull_secret_name(upstream: str, customer_name: str, project_name: str) -> str:
    """De naam van het pull-secret, expliciet gezet omdat de operator de suffix weglaat."""
    return sanitize_kubernetes_name(f"{organization_name(upstream, customer_name, project_name)}-{PULL_SECRET_POSTFIX}")


def registry_destination(upstream: str, registry_host: str, customer_name: str, project_name: str) -> str:
    """Waar images van deze upstream heen gaan: ``<rcr-host>/<organisatie>``."""
    return f"{registry_host}/{organization_name(upstream, customer_name, project_name)}"


def direct_secret_name(project_name: str, registry_name: str) -> str:
    """Het dockerconfigjson-secret op een cluster zonder proxy-operator, projectbreed."""
    return sanitize_kubernetes_name(f"{project_name}-{registry_name}-registry")
