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
