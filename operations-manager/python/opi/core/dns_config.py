"""DNS zones we administer ourselves, independent of any cluster.

A zone is not a cluster property: ``sandbox.rijksapp.dev`` (sandboxed-local) lives
inside the ``rijksapp.dev`` zone that odcn-production uses, so a per-cluster home
would give one zone two conflicting policies. This module is the single source.
"""

# Zone apex at TransIP -> the issuers allowed to issue for anything in it.
# Issuer names are the same ones ``nice_url.supported_domains`` uses.
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
# Declared, not derived: a list you can read beats a rule that is clever. Only the
# ``router.<zone>`` names are in here, and that is on purpose:
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
