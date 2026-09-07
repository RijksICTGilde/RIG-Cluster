"""Naamregels die alleen deze dienst kent.

Deze vier horen in het dienstpakket zelf en niet in de centrale ``opi/utils/naming.py``:
de maatstaf is dat je de map moet kunnen kopiëren, hernoemen en dat het dan werkt, en
naamregels die alleen over deze dienst gaan in een gedeeld bestand zetten is precies wat
dat onmogelijk maakt. De centrale helpers worden wél gebruikt, niet nagebouwd
(``publish_on_web/urls.py`` en ``vlam/endpoint.py`` doen dit al zo).

Het zijn **berekeningen, geen opgeslagen waarden**. Daarom bevat het projectbestand
alleen de invoer van de afnemer: alles wat wij eraan toevoegen zou een tweede waarheid
zijn die kan gaan afwijken van de organisatie zoals die er werkelijk staat.
"""

from __future__ import annotations

from opi.utils.naming import sanitize_kubernetes_name

#: Wat de operator zelf achter de organisatienaam plakt bij het pull-secret dat hij maakt.
#: Wij zetten ``robot.imagePullSecret.name`` expliciet, want de operator leidt die naam af
#: ZONDER ``spec.suffix`` -- gemeten op 2026-09-07 -- en dan botsen twee projecten met
#: dezelfde upstream op één naam, tenantbreed.
PULL_SECRET_POSTFIX = "robot-pull-secret"


def upstream_host(upstream: str) -> str:
    """De host uit een upstream-verwijzing: ``code.overheid.nl/robbert`` -> ``code.overheid.nl``."""
    return upstream.split("/", 1)[0]


def friendly_name(upstream: str) -> str:
    """De ``friendlyName`` voor de Quay-organisatie: de host zonder TLD en zonder punten.

    Quay verbiedt een punt in dat veld, dus er moet er iets met de punten gebeuren.
    ``code.overheid.nl`` wordt ``codeoverheid``: het laatste label (de TLD) valt weg en de
    rest wordt aan elkaar geschreven. Dat is niet alleen wat er in de proef op productie
    gemeten is, het is ook precies de vorm die de GEDEELDE proxy-organisaties op ODCN al
    hebben -- ``ghcr-rig``, ``gitlab-rig``, ``gcr-rig``, ``quay-rig``, ``code-overheid-rig``:
    in elk daarvan is de TLD weggelaten. Een naam die daarvan afwijkt zou naast de
    bestaande organisaties in dezelfde registry staan en er niet bij horen.

    Een poort valt weg, en een host zonder punt (``localhost``) blijft zoals hij is.
    """
    host = upstream_host(upstream).split(":", 1)[0]
    labels = host.split(".")
    stem = labels[:-1] if len(labels) > 1 else labels
    return sanitize_kubernetes_name("".join(stem))


def upstream_namespace(upstream: str) -> str:
    """Het pad achter de host: ``code.overheid.nl/robbert.uittenbroek`` -> ``robbert.uittenbroek``.

    Leeg als de upstream alleen een host is (``ghcr.io``).
    """
    _, _, path = upstream.partition("/")
    return path.strip("/")


def organization_suffix(upstream: str, project_name: str) -> str:
    """Wat wij als ``spec.suffix`` op de ``Organization`` zetten.

    De projectnaam PLUS de upstream-namespace, want D2 zegt één organisatie per project
    per upstream-NAMESPACE en de eerste twee delen van de naam die de operator samenstelt
    (``friendlyName``-``customerName``) dragen alleen de HOST. Zonder dit deel vallen
    ``ghcr.io/orga`` en ``ghcr.io/orgb`` van hetzelfde project op één organisatienaam, en
    daarmee op één bestandsnaam op het projectniveau, één credentials-secret en één
    bestemming -- de tweede registry overschrijft dan stil de eerste, en twee componenten
    die verschillende images bedoelen halen dezelfde op.

    Een upstream zonder pad houdt de kale projectnaam, zodat een registry op hostniveau
    dezelfde naam houdt die de proef op productie gemeten heeft.
    """
    namespace = upstream_namespace(upstream)
    return sanitize_kubernetes_name(f"{project_name}-{namespace}" if namespace else project_name)


def organization_name(upstream: str, customer_name: str, project_name: str) -> str:
    """De naam van de proxy-organisatie in RCR.

    ``<friendlyName>-<customerName>-<suffix>``: de eerste twee delen stelt de operator
    zelf samen uit ``spec.friendlyName`` en de klantnaam, het derde deel is onze
    ``spec.suffix`` (zie ``organization_suffix``). Eén organisatie per project per
    upstream-namespace (D2).
    """
    return sanitize_kubernetes_name(
        f"{friendly_name(upstream)}-{customer_name}-{organization_suffix(upstream, project_name)}"
    )


def pull_secret_name(upstream: str, customer_name: str, project_name: str) -> str:
    """De naam van het pull-secret dat bij die organisatie hoort.

    Wij zetten hem expliciet omdat de operator hem afleidt zonder de suffix. Hij draagt
    daarom de projectnaam, zodat twee projecten met dezelfde upstream niet op één naam
    botsen.
    """
    return sanitize_kubernetes_name(f"{organization_name(upstream, customer_name, project_name)}-{PULL_SECRET_POSTFIX}")


def registry_destination(upstream: str, registry_host: str, customer_name: str, project_name: str) -> str:
    """Waar images van deze upstream heen gaan: ``<rcr-host>/<organisatie>``.

    Samen met de upstream als ``match`` is dit de hele omzetting:
    ``code.overheid.nl/robbert/demo:tag`` wordt ``rcr.rijksapps.nl/<org>/demo:tag``, dus
    inclusief het wegvallen van het namespace-segment.
    """
    return f"{registry_host}/{organization_name(upstream, customer_name, project_name)}"


def direct_secret_name(project_name: str, registry_name: str) -> str:
    """De naam van het dockerconfigjson-secret op een cluster zonder proxy-operator.

    Namespace-scoped en projectbreed: het staat op het projectniveau in de
    deployments-repo, niet per deployment, want elke deployment van hetzelfde project in
    dezelfde namespace heeft hetzelfde secret nodig.
    """
    return sanitize_kubernetes_name(f"{project_name}-{registry_name}-registry")
