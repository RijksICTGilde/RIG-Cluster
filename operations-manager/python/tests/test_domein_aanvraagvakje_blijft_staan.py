"""De aanvraagvakjes bij een EIGEN domein: blijven ze staan, en wordt de aanvraag gedaan.

Gemeld tijdens de doorloop van RC-118: je kiest een eigen domein, vinkt "Domein aanvragen"
aan, en na het opslaan staat het vakje weer uit. De aanvraag werd ook nooit gedaan.

De oorzaak zit niet in het vakje maar in de VOLGORDE. Het keuzeveld slaat de schakelaar
``__custom__`` op en deferet het echte domein naar het transiente veld
``base-domain:custom``. Bij het RENDEREN is die deferral al opgelost, dus dan klopt alles.
Bij het VERWERKEN nog niet -- ``_resolve_deferrals`` draait pas na de veldenlus -- en
``_effective_base_domain`` gaf toen de sentinel terug. Beide aanvraag-condities slaan af op
``base_domain == "__custom__"``, dus gold het vakje als verborgen, sloeg de verwerker het
over en verdween het aangevinkte vakje. Daarmee liep ook de PRE_SAVE-hook niet die de
aanvraag doet.

Waarom dit niet eerder opviel: de bestaande tests voeden de conditie een deployment waarin
het domein AL is opgelost. Dat is de toestand na het opslaan, niet de toestand tijdens het
opslaan, en juist die tweede is waar het misging. Deze tests voeden daarom expliciet de
sentinel plus het transiente veld -- de vorm die de browser werkelijk instuurt.
"""

from __future__ import annotations

from unittest.mock import patch

from opi.forms.editables.conditions import (
    CUSTOM_BASE_DOMAIN_KEY,
    CUSTOM_DOMAIN_SENTINEL,
    DomainNeedsRequestCondition,
    SubdomainNeedsRequestCondition,
)

EIGEN_DOMEIN = "tweede-domein.nl"


def _deployment(base_domain, *, custom=None, subdomain=None) -> dict:
    """Een deployment zoals het formulier hem tijdens het verwerken aanlevert."""
    dep: dict = {
        "name": "productie",
        "services": [{"reference": "publish-on-web", "config": {"base-domain": base_domain}}],
    }
    if subdomain is not None:
        dep["services"][0]["config"]["subdomain"] = subdomain
    if custom is not None:
        dep[CUSTOM_BASE_DOMAIN_KEY] = custom
    return dep


def _yaml(dep: dict) -> dict:
    return {"name": "demo", "deployments": [dep]}


class TestDomeinAanvraagvakje:
    """De toestand TIJDENS het verwerken: sentinel opgeslagen, domein in het transiente veld."""

    def test_vakje_is_zichtbaar_bij_een_eigen_domein_dat_nog_niet_is_opgelost(self):
        data = _yaml(_deployment(CUSTOM_DOMAIN_SENTINEL, custom=EIGEN_DOMEIN))

        assert DomainNeedsRequestCondition().check(data) is True

    def test_vakje_is_zichtbaar_als_het_domein_al_is_opgelost(self):
        """De toestand NA het opslaan blijft werken zoals hij deed."""
        data = _yaml(_deployment(EIGEN_DOMEIN))

        assert DomainNeedsRequestCondition().check(data) is True

    def test_sentinel_zonder_ingevuld_domein_toont_het_vakje_niet(self):
        """Alleen de schakelaar, nog geen domein getypt: er valt niets aan te vragen."""
        data = _yaml(_deployment(CUSTOM_DOMAIN_SENTINEL))

        assert DomainNeedsRequestCondition().check(data) is False

    def test_een_leeg_custom_veld_telt_niet_als_domein(self):
        data = _yaml(_deployment(CUSTOM_DOMAIN_SENTINEL, custom=""))

        assert DomainNeedsRequestCondition().check(data) is False

    def test_een_al_goedgekeurd_eigen_domein_hoeft_niet_opnieuw(self):
        data = _yaml(_deployment(CUSTOM_DOMAIN_SENTINEL, custom=EIGEN_DOMEIN))
        data["domains"] = {"allowed-domains": [{"domain": EIGEN_DOMEIN, "status": "approved"}]}

        assert DomainNeedsRequestCondition().check(data) is False


class TestSubdomeinAanvraagvakje:
    """Hetzelfde vakje, maar voor een subdomein op een beperkt eigen domein."""

    def _restricted(self):
        """Een eigen domein met beperkte subdomeinen, nog niet goedgekeurd."""
        return patch(
            "opi.forms.editables.conditions.get_project_allowed_domain_config",
            return_value={"domain": EIGEN_DOMEIN, "restricted-subdomains": True},
        )

    def test_vakje_is_zichtbaar_bij_een_nog_niet_opgelost_eigen_domein(self):
        data = _yaml(_deployment(CUSTOM_DOMAIN_SENTINEL, custom=EIGEN_DOMEIN, subdomain="acceptatie"))
        with (
            self._restricted(),
            patch("opi.forms.editables.conditions.get_supported_base_domains", return_value=[]),
            patch("opi.forms.editables.conditions.get_subdomain_status", return_value="requested"),
        ):
            assert SubdomainNeedsRequestCondition().check(data) is True

    def test_zonder_subdomein_geen_vakje(self):
        data = _yaml(_deployment(CUSTOM_DOMAIN_SENTINEL, custom=EIGEN_DOMEIN))
        with (
            self._restricted(),
            patch("opi.forms.editables.conditions.get_supported_base_domains", return_value=[]),
        ):
            assert SubdomainNeedsRequestCondition().check(data) is False

    def test_een_al_goedgekeurd_subdomein_hoeft_niet_opnieuw(self):
        data = _yaml(_deployment(CUSTOM_DOMAIN_SENTINEL, custom=EIGEN_DOMEIN, subdomain="acceptatie"))
        with (
            self._restricted(),
            patch("opi.forms.editables.conditions.get_supported_base_domains", return_value=[]),
            patch("opi.forms.editables.conditions.get_subdomain_status", return_value="approved"),
        ):
            assert SubdomainNeedsRequestCondition().check(data) is False
