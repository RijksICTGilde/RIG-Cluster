"""De driewegmerge van de store moet over INHOUD gaan, niet over containervormen.

Gemeten in de doorloop van RC-118: de domeinwizard gaf permanent "Project is gewijzigd
sinds je begon met bewerken", terwijl er niemand anders schreef en het bestand aantoonbaar
niet bewoog. De oorzaak zit in ``_apply_our_change_to``:

- ``theirs`` komt uit ``_read_committed`` en is een ruamel ``CommentedMap``;
- ``base`` kan ook een ``CommentedMap`` zijn (via de cache uit een YAML-herlaad), en droeg
  in het gemeten geval bovendien een transient veld dat git nooit heeft gezien;
- ``ours`` is de wizard-uitvoer: een platte ``dict``.

``CommentedMap`` is een dict-subklasse, dus ``==`` is er blind voor -- maar ``DeepDiff``
niet. Die ziet ``old_type CommentedMap, new_type dict`` op de ROOT, stopt met afdalen, en
maakt van de hele wijziging een document-vervanging. Zo'n delta verifieert zijn
``old_value`` tegen ``theirs`` (bidirectional), en die komt nooit overeen zodra base ook
maar een sleutel draagt die theirs niet heeft. ``DeltaError`` -> ``None`` -> ConflictError,
en bij elke nieuwe poging opnieuw, want de vormen veranderen niet.

De fix normaliseert de DIFF-invoer naar platte containers (en ruamel-stringsubklassen naar
``str``), zodat de diff granulair blijft. De delta wordt op de ECHTE ``theirs`` toegepast;
``==``-verificatie binnen de delta is container-blind, dus dat past.

De grenzen staan hier ook vast: een echt conflict (twee kanten wijzigen hetzelfde veld
verschillend) moet ``None`` BLIJVEN. Normaliseren mag nooit gelijkheid verzinnen.
"""

from __future__ import annotations

import io

from opi.services.project_store import _apply_our_change_to
from ruamel.yaml import YAML
from ruamel.yaml.scalarstring import LiteralScalarString

_PROJECT_YAML = (
    "name: demo\n"
    "deployments:\n"
    "  - name: productie\n"
    "    services:\n"
    "      - reference: publish-on-web\n"
    "        config:\n"
    "          base-domain: cluster.nl\n"
)


def _commented() -> dict:
    """Het projectbestand zoals ``_read_committed`` het leest: een CommentedMap."""
    return YAML().load(io.StringIO(_PROJECT_YAML))


def _plain(domain: str = "cluster.nl") -> dict:
    """Dezelfde inhoud als platte dict, zoals de wizard hem aanlevert."""
    return {
        "name": "demo",
        "deployments": [
            {
                "name": "productie",
                "services": [{"reference": "publish-on-web", "config": {"base-domain": domain}}],
            }
        ],
    }


class TestDeGemetenProductievorm:
    def test_commentedmap_basis_met_transient_tegen_platte_ours_merget(self):
        """De exacte vorm uit de RC118-diagnose. Voor de fix: None, dus vals conflict."""
        base = _commented()
        base["deployments"][0]["base-domain:custom"] = "tweede-domein.nl"
        ours = _plain("eigen-domein.nl")

        merged = _apply_our_change_to(base=base, ours=ours, theirs=_commented())

        assert merged is not None, "inhoudelijk niet-conflicterende edit werd als conflict gemeld"
        assert merged["deployments"][0]["services"][0]["config"]["base-domain"] == "eigen-domein.nl"

    def test_de_wijziging_landt_en_het_transient_lift_niet_mee(self):
        base = _commented()
        base["deployments"][0]["base-domain:custom"] = "tweede-domein.nl"

        merged = _apply_our_change_to(base=base, ours=_plain("eigen-domein.nl"), theirs=_commented())

        assert merged is not None
        assert "base-domain:custom" not in merged["deployments"][0]

    def test_alleen_de_vormwissel_zonder_transient_merget_ook(self):
        """CommentedMap-basis, platte ours, geen transient: puur de containervorm."""
        merged = _apply_our_change_to(base=_commented(), ours=_plain("eigen-domein.nl"), theirs=_commented())

        assert merged is not None
        assert merged["deployments"][0]["services"][0]["config"]["base-domain"] == "eigen-domein.nl"

    def test_ruamel_stringsubklassen_tellen_niet_als_wijziging(self):
        """Een LiteralScalarString (AGE-blok in het bestand) is dezelfde str-inhoud."""
        blok = "-----BEGIN AGE ENCRYPTED FILE-----\nAAA\n-----END AGE ENCRYPTED FILE-----"
        base = _plain()
        base["config"] = {"api-key": LiteralScalarString(blok)}
        ours = _plain("eigen-domein.nl")
        ours["config"] = {"api-key": blok}
        theirs = _commented()
        theirs["config"] = {"api-key": LiteralScalarString(blok)}

        merged = _apply_our_change_to(base=base, ours=ours, theirs=theirs)

        assert merged is not None
        assert merged["deployments"][0]["services"][0]["config"]["base-domain"] == "eigen-domein.nl"
        assert str(merged["config"]["api-key"]) == blok


class TestEchteConflictenBlijvenConflicten:
    """Normaliseren mag alleen vormen gelijktrekken, nooit inhoud."""

    def test_zelfde_veld_verschillend_gewijzigd_blijft_none(self):
        base = _commented()
        ours = _plain("van-ons.nl")
        theirs = _commented()
        theirs["deployments"][0]["services"][0]["config"]["base-domain"] = "van-de-ander.nl"

        assert _apply_our_change_to(base=base, ours=ours, theirs=theirs) is None

    def test_beide_kanten_voegen_dezelfde_nieuwe_sleutel_verschillend_toe(self):
        base = _commented()
        ours = _plain()
        ours["deployments"][0]["services"][0]["config"]["issuer"] = "letsencrypt"
        theirs = _commented()
        theirs["deployments"][0]["services"][0]["config"]["issuer"] = "eigen-ca"

        assert _apply_our_change_to(base=base, ours=ours, theirs=theirs) is None

    def test_niet_overlappende_wijzigingen_overleven_allebei(self):
        """De reden dat dit een structurele merge is en geen git-merge: beide blijven."""
        base = _commented()
        ours = _plain("eigen-domein.nl")
        theirs = _commented()
        theirs["deployments"][0]["namespace"] = "demo-ns"

        merged = _apply_our_change_to(base=base, ours=ours, theirs=theirs)

        assert merged is not None
        assert merged["deployments"][0]["services"][0]["config"]["base-domain"] == "eigen-domein.nl"
        assert merged["deployments"][0]["namespace"] == "demo-ns"
