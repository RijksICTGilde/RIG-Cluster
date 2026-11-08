"""DNS zones we administer ourselves, independent of any cluster.

A zone is not a cluster property: ``sandbox.rijksapp.dev`` (sandboxed-local) lives
inside the ``rijksapp.dev`` zone that odcn-production uses, so a per-cluster home
would give one zone two conflicting policies. This module is the single source.
"""

# Zone apex at TransIP -> the issuers allowed to issue for anything in it.
# Issuer names are the same ones ``domains.supported_domains`` uses.
MANAGED_DNS_ZONES: dict[str, list[str]] = {
    "rijks.app": ["letsencrypt"],
    "rijksapp.nl": ["letsencrypt"],
    "rijksapp.dev": ["letsencrypt"],
}

# Issuer name -> the CAA issuer-domain-name that CA publishes.
CAA_IDENTIFIERS: dict[str, str] = {
    "letsencrypt": "letsencrypt.org",
}

# The Baseline Requirements let a CA reuse a CAA result for the TTL or 8 hours,
# whichever is greater. Below 8h buys nothing; above it only lengthens how long a
# mistake sticks around.
CAA_TTL = 3600

# ``issuewild`` goes along because the sandbox serves *.sandbox.rijksapp.dev from a
# wildcard certificate: a zone with only ``issue`` forbids the next wildcard issuance.
# No ``iodef``: that record points CA abuse reports at a mailbox, and an unattended
# mailbox there is worse than no record at all.
CAA_TAGS = ("issue", "issuewild")


# Names we publish ourselves that never send and never receive mail, per managed zone.
# Relative names, as TransIP writes them; "@" would be the apex.
#
# Declared, not derived. Only the ``router.<zone>`` names are in here:
# - everything else we publish under these zones (zad, keycloak, project subdomains) is
#   a CNAME to one of these routers, and TransIP refuses a record next to a CNAME;
# - the three apexes already carry "v=spf1 -all" and "v=DMARC1; p=reject; sp=reject".
#   A null MX there would say the whole zone accepts no mail, and that is a policy call
#   about the zone, not the hygiene fix this list is for.
NO_MAIL_NAMES: dict[str, list[str]] = {
    "rijks.app": ["router"],
    "rijksapp.nl": ["router"],
    "rijksapp.dev": ["router"],
}

# Same reasoning as CAA_TTL: long enough to be cheap, short enough that a mistake ages out.
NO_MAIL_TTL = 3600

# SPF that authorizes nobody, the null MX of RFC 7505, and a DMARC policy that rejects.
# Together they say: no mail leaves this name, and no mail is accepted for it.
SPF_CONTENT = "v=spf1 -all"
NULL_MX_CONTENT = "0 ."
DMARC_CONTENT = "v=DMARC1; p=reject;"

# DMARC lives on a child of the name it protects.
DMARC_PREFIX = "_dmarc"


def no_mail_names(zone: str) -> list[str]:
    """Relative names in a managed zone that must carry the no-mail policy."""
    if zone not in MANAGED_DNS_ZONES:
        msg = f"Zone '{zone}' is not a managed DNS zone"
        raise ValueError(msg)
    return list(NO_MAIL_NAMES.get(zone, []))


def desired_caa_contents(zone: str) -> list[str]:
    """CAA RDATA strings for a managed zone's apex, in a stable order."""
    issuers = MANAGED_DNS_ZONES.get(zone)
    if issuers is None:
        msg = f"Zone '{zone}' is not a managed DNS zone"
        raise ValueError(msg)

    contents: list[str] = []
    for tag in CAA_TAGS:
        for issuer in issuers:
            identifier = CAA_IDENTIFIERS.get(issuer)
            if identifier is None:
                msg = f"Issuer '{issuer}' on zone '{zone}' has no CAA identifier"
                raise ValueError(msg)
            contents.append(f'0 {tag} "{identifier}"')
    return contents


# The bare entrances of the cluster: every other name we publish is a CNAME to one of these.
# They carry no application, so whoever asks for one in a browser gets the explanation about
# pointing an own domain at it (see opi/web/router.py).
#
# Derived from MANAGED_DNS_ZONES and not written out a second time, so adding a zone needs
# no edit here.
# The sandbox is listed separately: `sandbox.rijksapp.dev` is not a zone of its own at
# TransIP but a piece of `rijksapp.dev` (see the head of this file), so deriving it from
# MANAGED_DNS_ZONES does not produce it. Without this line the sandbox shows the explanation
# with production names in it.
SANDBOX_ROUTER_HOSTNAME = "router.sandbox.rijksapp.dev"

ROUTER_HOSTNAMES: frozenset[str] = frozenset(
    [f"router.{zone}" for zone in MANAGED_DNS_ZONES] + [SANDBOX_ROUTER_HOSTNAME]
)

# The zones behind those names, LONGEST FIRST: `zad.sandbox.rijksapp.dev` ends in both
# `sandbox.rijksapp.dev` and `rijksapp.dev`, and has to get the first one.
ROUTER_ZONES: tuple[str, ...] = tuple(
    sorted((name.removeprefix("router.") for name in ROUTER_HOSTNAMES), key=len, reverse=True)
)

# The addresses those names point at. They are on the explanation page because somebody
# retypes them in their own zone, so they have to match what is in TransIP.
ROUTER_IPV4 = "147.181.48.71"
ROUTER_IPV6 = "2a04:9a00:1007:4000:0:2:0:8"

# Per router name, because they do not hold everywhere. The SANDBOX is deliberately not in
# here: router.sandbox.rijksapp.dev points at 127.0.0.1 (measured 19 August 2026), and
# showing the production addresses on a page that promises to say only what is true is worse
# than leaving them out. Without addresses the page shows the CNAME form only, and that one
# is right there.
ROUTER_ADDRESSES: dict[str, tuple[str, str]] = {
    "router.rijksapp.nl": (ROUTER_IPV4, ROUTER_IPV6),
    "router.rijks.app": (ROUTER_IPV4, ROUTER_IPV6),
    "router.rijksapp.dev": (ROUTER_IPV4, ROUTER_IPV6),
}


def router_addresses_for(router_host: str) -> tuple[str, str] | None:
    """The A and AAAA value for a router name, or None when we do not know them for it."""
    return ROUTER_ADDRESSES.get(router_host)


# The router name the portal names when somebody asks for the explanation on a plain name.
DEFAULT_ROUTER_HOSTNAME = "router.rijksapp.nl"


def router_hostname_for(host: str | None) -> str:
    """The router name that belongs with a host name.

    The explanation page names a concrete name the visitor retypes in their own zone, and
    that has to belong to the zone they are looking at: whoever is on rijks.app has no use
    for router.rijksapp.nl. On the router name itself the answer is that name; on every other
    name the router of the same zone, and otherwise the default.
    """
    if not host:
        return DEFAULT_ROUTER_HOSTNAME
    if host in ROUTER_HOSTNAMES:
        return host
    for zone in ROUTER_ZONES:
        if host == zone or host.endswith(f".{zone}"):
            return f"router.{zone}"
    return DEFAULT_ROUTER_HOSTNAME
