"""Dekking voor :func:`opi.core.leden.ledenlijst_wijzigt`.

Deze functie staat sinds RC-392 op een keer, maar wordt aangeroepen door drie wegen die
leden kunnen schrijven (de step-up-detectie, de team-wizard-guard en de rolpoort in de
bewerkwizard). Een regressie hier raakt alle drie in een keer, dus de functie heeft zijn
eigen test op zijn randen.
"""

from __future__ import annotations

from opi.core.leden import ledenlijst_wijzigt

OWNER = {"email": "owner@example.com", "roles": ["admin", "ops"]}
MEMBER = {"email": "member@example.com", "roles": ["viewer"]}


def test_geen_wijziging_is_geen_wijziging() -> None:
    before = {"users": [OWNER, MEMBER]}
    after = {"users": [dict(OWNER), dict(MEMBER)]}
    assert ledenlijst_wijzigt(before, after) is False


def test_een_rol_erbij_is_een_wijziging() -> None:
    before = {"users": [OWNER, MEMBER]}
    after = {"users": [OWNER, {**MEMBER, "roles": ["viewer", "developer"]}]}
    assert ledenlijst_wijzigt(before, after) is True


def test_een_lid_erbij_is_een_wijziging() -> None:
    before = {"users": [OWNER]}
    after = {"users": [OWNER, MEMBER]}
    assert ledenlijst_wijzigt(before, after) is True


def test_een_lid_eraf_is_een_wijziging() -> None:
    before = {"users": [OWNER, MEMBER]}
    after = {"users": [OWNER]}
    assert ledenlijst_wijzigt(before, after) is True


def test_volgorde_van_leden_maakt_niets_uit() -> None:
    before = {"users": [OWNER, MEMBER]}
    after = {"users": [MEMBER, OWNER]}
    assert ledenlijst_wijzigt(before, after) is False


def test_volgorde_van_rollen_binnen_een_lid_maakt_niets_uit() -> None:
    before = {"users": [{"email": "a@example.com", "roles": ["admin", "ops"]}]}
    after = {"users": [{"email": "a@example.com", "roles": ["ops", "admin"]}]}
    assert ledenlijst_wijzigt(before, after) is False


def test_een_lid_zonder_email_valt_terug_op_name() -> None:
    before = {"users": [{"name": "service-account-1", "roles": ["developer-ci"]}]}
    after = {"users": [{"name": "service-account-1", "roles": ["developer-ci"]}]}
    assert ledenlijst_wijzigt(before, after) is False


def test_niet_dict_leden_worden_genegeerd() -> None:
    before = {"users": [OWNER, "kapotte-waarde"]}
    after = {"users": [OWNER]}
    assert ledenlijst_wijzigt(before, after) is False


def test_ontbrekende_users_sleutel_is_een_lege_lijst() -> None:
    assert ledenlijst_wijzigt({}, {}) is False
    assert ledenlijst_wijzigt({}, {"users": [OWNER]}) is True


def test_users_none_is_een_lege_lijst() -> None:
    assert ledenlijst_wijzigt({"users": None}, {"users": [OWNER]}) is True
