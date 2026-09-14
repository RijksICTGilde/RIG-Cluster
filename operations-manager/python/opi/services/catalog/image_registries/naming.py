"""Naamregels die alleen deze dienst kent, zodat de map verplaatsbaar blijft."""

from __future__ import annotations

import hashlib

from opi.utils.naming import sanitize_kubernetes_name

#: De operator plakt dit achter de naam van het pull-secret dat hij maakt.
PULL_SECRET_POSTFIX = "robot-pull-secret"

#: De gebruikersnaam die wij invullen als de afnemer er geen opgaf.
#:
#: Er MOET iets staan, en dat is geen keuze van ons: een ``kubernetes.io/dockerconfigjson``
#: draagt per registry een ``auth`` van ``base64(gebruikersnaam:wachtwoord)``. Er is geen
#: veld voor alleen een token, dus een leeg linkerdeel zou ``:token`` opleveren, en dat
#: weigeren registries als een lege gebruikersnaam.
#:
#: Wat de waarde IS doet er bij de meeste registries niet toe zolang het token klopt
#: (ghcr.io kijkt er niet naar); waar hij er wel toe doet -- Docker Hub, Quay -- moet de
#: afnemer hem zelf invullen. Laat hij hem daar leeg, dan komt de plaatshouder in het
#: pull-secret en weigert de registry de pull; de tokentoets (``enforcers.py``) meldt dat
#: alleen onder zijn eigen voorwaarden. ``x-access-token`` is de naam die GitLab en GitHub
#: in hun eigen documentatie gebruiken voor precies dit gat.
#:
#: Hij wordt berekend, niet opgeslagen: het projectbestand draagt alleen wat de afnemer
#: heeft ingevuld, net als bij de RCR-URL en de secretnaam hieronder. Laat de afnemer het
#: veld leeg, dan blijft het leeg in de config en ontstaat deze waarde pas op het moment
#: dat de dockerconfigjson wordt gebouwd.
PULL_USERNAME_PLACEHOLDER = "x-access-token"

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


#: Hoeveel varianten we proberen voor we het opgeven; met meer registries dan dit in een
#: project is er iets anders aan de hand dan een naambotsing.
SLUG_ATTEMPTS = 100


def registry_slug(label: str, taken: set[str]) -> str:
    """De verwijzing die bij een vrij label hoort, uniek binnen het project.

    De afnemer typt "Code Overheid" en niet "code-overheid": een DNS-label is onze eis en
    niet zijn probleem. Dezelfde vorm die het project zelf al heeft met ``name`` plus
    ``display-name``, een niveau lager.

    De slug is BEVROREN zodra hij bestaat -- deze functie draait alleen voor een entry die
    er nog geen heeft (zie ``ImageRegistriesService.generate_missing_values``). Hij is de
    verwijzing vanaf componenten en hij zit in de naam van het dockerconfigjson-secret,
    dus een label dat later verandert mag hem niet meenemen.

    ``REGISTRY_NAME_PATTERN`` eist een kleine LETTER vooraan, zodat de naam nooit als
    YAML-getal wordt gelezen; een label dat met een cijfer begint krijgt daarom een ``r``.
    Dat prefix schuift de grens een teken naar links, en het afkappen daarna kan dus weer
    op een streepje eindigen -- wat het patroon weigert, op een veld dat de afnemer sinds
    RC-187 niet meer ziet. Daarom hier de opschoning van ``sanitize_kubernetes_name``
    herhalen op de nieuwe grens.
    """
    basis = sanitize_kubernetes_name(label, max_length=60)
    if not basis[0].isalpha():
        basis = f"r{basis}"[:60].rstrip("-")
    if basis not in taken:
        return basis
    for volgnummer in range(2, SLUG_ATTEMPTS + 1):
        kandidaat = f"{basis}-{volgnummer}"
        if kandidaat not in taken:
            return kandidaat
    msg = f"Geen vrije registrynaam te maken voor label '{label}'"
    raise ValueError(msg)
