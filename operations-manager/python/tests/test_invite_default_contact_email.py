"""``default_contact_email`` slaat een service account over.

Sinds RC-322 draagt ``users`` ook service accounts, die geen postvak hebben. Zonder de
filter uit ``opi.services.project_leden.menselijke_leden_uit_data`` zou de eerste rij in
de ledenlijst zomaar een service account kunnen zijn, en dan levert de default een adres
op dat nergens aankomt.
"""

from __future__ import annotations

from opi.services.catalog.invite.defaults import default_contact_email


def test_de_eerste_mens_wordt_het_contactadres() -> None:
    yaml_data = {"users": [{"email": "eerste@rijksoverheid.nl", "roles": ["admin", "ops"]}]}

    assert default_contact_email(yaml_data) == "eerste@rijksoverheid.nl"


def test_een_service_account_voor_de_eerste_mens_wordt_overgeslagen() -> None:
    """Het service account staat als eerste in de lijst en draagt hier zelfs een (nep)
    e-mailveld: zonder de typefilter zou dat ten onrechte het contactadres worden, want
    een service account heeft geen postvak dat mail ontvangt."""
    yaml_data = {
        "users": [
            {"type": "service-account", "name": "bouwstraat", "roles": ["developer"], "email": "bouwstraat@nergens"},
            {"email": "tweede@rijksoverheid.nl", "roles": ["admin", "ops"]},
        ]
    }

    assert default_contact_email(yaml_data) == "tweede@rijksoverheid.nl"


def test_alleen_service_accounts_geeft_geen_contactadres() -> None:
    yaml_data = {"users": [{"type": "service-account", "name": "bouwstraat", "roles": ["developer"]}]}

    assert default_contact_email(yaml_data) is None


def test_zonder_ledenlijst_geeft_geen_contactadres() -> None:
    assert default_contact_email({}) is None
