"""Een lid, zijn soort en zijn tokens staan in het projectbestand.

Daar en niet in een tabel, omdat lidmaatschap deel is van de SAMENSTELLING van een project:
zo is het reviewbaar en staat het in de historie. Een token hoort bij een lid, dus het
staat erbij; intrekken is daarmee een commit.

Wat hier wordt vastgelegd:

- een service account is een lid met een naam in plaats van een e-mailadres, nooit allebei;
- een token draagt alleen een hash, een naam en een verplichte vervaldatum;
- de migratie naar 2.12 verplaatst ``config.api-key`` naar het service account
  ``project-api`` zonder de waarde te veranderen, en is idempotent.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from opi.core.leden import LidSoort
from opi.core.project_schema import ProjectSchemaError, validate_project_schema
from opi.forms.models.project_file import ProjectUserModel
from opi.services.project_leden import hash_van, is_verlopen, service_accounts
from opi.services.project_service import ProjectSummary, ProjectUser
from opi.services.schema_migration import (
    LATEST_SCHEMA_VERSION_VALUE,
    PROJECT_API_ACCOUNT,
    migrate_to_latest,
)
from pydantic import ValidationError

EEN_HASH = "a" * 64


def _bestand(users: list[dict], **extra) -> dict:
    basis = {
        "schema-version": LATEST_SCHEMA_VERSION_VALUE,
        "name": "bouwput",
        "display-name": "Bouwput",
        "clusters": ["odcn-production"],
        "users": users,
    }
    basis.update(extra)
    return basis


# --- het schema ------------------------------------------------------------


def test_een_mens_en_een_service_account_staan_in_dezelfde_lijst() -> None:
    validate_project_schema(
        _bestand(
            [
                {"email": "beheerder@rijksoverheid.nl", "roles": ["admin", "ops"]},
                {"type": "service-account", "name": "bouwstraat", "roles": ["developer"]},
            ]
        )
    )


def test_een_service_account_heeft_geen_e_mailadres() -> None:
    """Een adres op een service account zou hem als mens laten doorgaan bij elke lezer
    die alleen naar ``email`` kijkt, en dat is precies de verwarring die
    ``human_members`` moet voorkomen."""
    with pytest.raises(ProjectSchemaError):
        validate_project_schema(
            _bestand([{"type": "service-account", "name": "bouwstraat", "roles": ["admin", "ops"], "email": "x@y.nl"}])
        )


def test_een_mens_heeft_geen_naam() -> None:
    with pytest.raises(ProjectSchemaError):
        validate_project_schema(_bestand([{"email": "a@b.nl", "roles": ["admin", "ops"], "name": "bouwstraat"}]))


def test_een_service_accountnaam_is_een_dns_label() -> None:
    """De naam reist mee in de actor en in de git-trailer, dus hij moet overal te schrijven zijn."""
    with pytest.raises(ProjectSchemaError):
        validate_project_schema(
            _bestand([{"type": "service-account", "name": "Bouw Straat", "roles": ["admin", "ops"]}])
        )


def test_een_token_draagt_een_naam_een_vervaldatum_en_een_hash() -> None:
    validate_project_schema(
        _bestand(
            [
                {
                    "email": "a@b.nl",
                    "roles": ["admin", "ops"],
                    "tokens": [{"name": "cli", "expires": "2027-01-01T00:00:00+00:00", "hash": EEN_HASH}],
                }
            ]
        )
    )


def test_een_token_zonder_vervaldatum_bestaat_niet() -> None:
    """Een uitzondering die eenmaal bestaat wordt de norm, dus die is er niet."""
    with pytest.raises(ProjectSchemaError):
        validate_project_schema(
            _bestand([{"email": "a@b.nl", "roles": ["admin", "ops"], "tokens": [{"name": "cli", "hash": EEN_HASH}]}])
        )


def test_een_token_draagt_nooit_de_waarde_zelf() -> None:
    """``additionalProperties: false`` is hier de hele bescherming: een veld ``value``
    zou een leesbaar geheim in de git-historie zetten."""
    with pytest.raises(ProjectSchemaError):
        validate_project_schema(
            _bestand(
                [
                    {
                        "email": "a@b.nl",
                        "roles": ["admin", "ops"],
                        "tokens": [
                            {
                                "name": "cli",
                                "expires": "2027-01-01T00:00:00+00:00",
                                "hash": EEN_HASH,
                                "value": "zad_geheim",
                            }
                        ],
                    }
                ]
            )
        )


def test_een_niet_verplaatste_projectsleutel_houdt_het_bestand_geldig() -> None:
    """2.12 laat ``config.api-key`` nog toe, en dat is met opzet.

    De migratie haalt het veld weg zodra ze de sleutel open krijgt. Lukt dat niet (geen
    platform-KEK), dan blijft hij staan, en een schema dat hem dan afkeurt laat elke
    verwerking van dat bestand stil stranden: precies de klasse fouten die de
    upgrade-safety-replay moet vinden.
    """
    validate_project_schema(
        _bestand(
            [{"email": "a@b.nl", "roles": ["admin", "ops"]}],
            config={"api-key": "base64+age:ZmFrZQ=="},
        )
    )


# --- het Pydantic-model ----------------------------------------------------


def test_het_model_eist_een_naam_bij_een_service_account() -> None:
    with pytest.raises(ValidationError):
        ProjectUserModel(type=LidSoort.SERVICE_ACCOUNT, role="admin")


def test_het_model_eist_een_e_mailadres_bij_een_mens() -> None:
    with pytest.raises(ValidationError):
        ProjectUserModel(role="admin")


# --- de migratie naar 2.12 -------------------------------------------------


def _op_2_11(api_key: str = "de-oude-sleutel") -> dict:
    return {
        "schema-version": "2.11",
        "name": "bouwput",
        "users": [{"email": "beheerder@rijksoverheid.nl", "roles": ["admin", "ops"]}],
        "config": {"age-public-key": "age1x", "api-key": api_key},
    }


def test_de_projectsleutel_wordt_een_token_van_project_api() -> None:
    """Dezelfde waarde blijft werken, dus een draaiende pijplijn merkt niets."""
    gemigreerd, veranderd = migrate_to_latest(_op_2_11())

    assert veranderd
    assert "api-key" not in gemigreerd["config"]

    account = next(lid for lid in gemigreerd["users"] if lid.get("name") == PROJECT_API_ACCOUNT)
    assert account["type"] == LidSoort.SERVICE_ACCOUNT.value
    # 2.12 schrijft role: admin; de stap naar 2.13 maakt er [admin, ops] van.
    assert account["roles"] == ["admin", "ops"]
    assert account["tokens"][0]["hash"] == hash_van("de-oude-sleutel")
    validate_project_schema(gemigreerd)


def test_de_migratie_is_idempotent() -> None:
    """Hij draait bij ELKE lezing van een bestand dat nog niet is weggeschreven, dus een
    tweede ronde mag geen tweede account en geen tweede token opleveren."""
    eerste, _ = migrate_to_latest(_op_2_11())
    tweede, _ = migrate_to_latest(dict(eerste))

    assert tweede == eerste
    accounts = [lid for lid in tweede["users"] if lid.get("name") == PROJECT_API_ACCOUNT]
    assert len(accounts) == 1
    assert len(accounts[0]["tokens"]) == 1


def test_een_bestand_zonder_projectsleutel_verandert_niet() -> None:
    zonder = {
        "schema-version": "2.11",
        "name": "bouwput",
        "users": [{"email": "a@b.nl", "roles": ["admin", "ops"]}],
        "config": {"age-public-key": "age1x"},
    }
    gemigreerd, _ = migrate_to_latest(zonder)
    assert all(lid.get("name") != PROJECT_API_ACCOUNT for lid in gemigreerd["users"])


def _op_2_11_versleuteld() -> dict:
    """De vorm die elk productiebestand heeft: ``api-key`` AGE-versleuteld met de
    projectsleutel, die zelf weer ingepakt is met de KEK. Het plaintext-token hierboven
    dekt de doorgeefvorm van ``is_age_encrypted``, niet het echte ontsleutelpad."""
    return {
        "schema-version": "2.11",
        "name": "bouwput",
        "users": [{"email": "beheerder@rijksoverheid.nl", "roles": ["admin", "ops"]}],
        "config": {
            "age-public-key": "age1x",
            "age-private-key": "-----BEGIN AGE ENCRYPTED FILE-----\ningepakte-sleutel\n-----END AGE ENCRYPTED FILE-----",
            "api-key": "-----BEGIN AGE ENCRYPTED FILE-----\nversleutelde-sleutel\n-----END AGE ENCRYPTED FILE-----",
        },
    }


def test_een_versleutelde_projectsleutel_wordt_eerst_ontsleuteld(monkeypatch: pytest.MonkeyPatch) -> None:
    """De hash moet over de PLATTE waarde, want dat is wat een aanroeper presenteert.

    Hashen van de ciphertext zou een token opleveren dat de oude, platte sleutel nooit
    verifieert: precies de regressie die de migratie voor een draaiende pijplijn voorkomt.
    """
    import opi.services.schema_migration as sm

    monkeypatch.setattr(sm, "unwrap_project_key_sync", lambda *_: "projectsleutel-plat")
    monkeypatch.setattr(sm, "decrypt_age_content_sync", lambda *_: "de-echte-platte-sleutel")

    gemigreerd, veranderd = migrate_to_latest(_op_2_11_versleuteld())

    assert veranderd
    assert "api-key" not in gemigreerd["config"]
    account = next(lid for lid in gemigreerd["users"] if lid.get("name") == PROJECT_API_ACCOUNT)
    assert account["tokens"][0]["hash"] == hash_van("de-echte-platte-sleutel")


def test_een_niet_te_ontsleutelen_projectsleutel_blijft_staan(monkeypatch: pytest.MonkeyPatch) -> None:
    """Afwijking bewust: weghalen zou de pijplijn buitensluiten op een sleutel die we niet
    konden lezen. Het bestand blijft GELDIG, zodat de verwerking doorloopt en de migratie
    het bij een volgende ronde opnieuw probeert."""
    import opi.services.schema_migration as sm

    monkeypatch.setattr(sm, "unwrap_project_key_sync", lambda *_: (_ for _ in ()).throw(ValueError("verkeerde kek")))

    bestand = _op_2_11_versleuteld()
    gemigreerd, _ = migrate_to_latest(bestand)

    assert gemigreerd["config"]["api-key"] == bestand["config"]["api-key"]
    assert all(lid.get("name") != PROJECT_API_ACCOUNT for lid in gemigreerd["users"])
    validate_project_schema(gemigreerd)


# --- is_verlopen en service_accounts ---------------------------------------


def test_is_verlopen_behandelt_een_naieve_datum_als_utc() -> None:
    """Een vervaldatum zonder tijdzone komt niet voor in een echt projectbestand (het
    schema eist een offset), maar deze functie wordt ook rechtstreeks op datetime-objecten
    aangeroepen en moet dan niet op een vergelijking tussen naive en aware knappen."""
    verleden_naief = datetime(2020, 1, 1)  # noqa: DTZ001 -- juist de naive vorm is hier het doel
    toekomst_naief = datetime(2999, 1, 1)  # noqa: DTZ001 -- idem

    assert is_verlopen(verleden_naief) is True
    assert is_verlopen(toekomst_naief) is False


def test_service_accounts_laat_alleen_de_service_accounts_zien() -> None:
    project = ProjectSummary(
        name="bouwput",
        filename="projects/bouwput.yaml",
        users=[
            ProjectUser(email="mens@rijksoverheid.nl", role="admin"),
            ProjectUser(type=LidSoort.SERVICE_ACCOUNT, name="bouwstraat", role="developer"),
        ],
    )

    assert [lid.name for lid in service_accounts(project)] == ["bouwstraat"]


def test_project_user_eist_een_naam_bij_een_service_account() -> None:
    """Zonder deze eis matcht een lid met een leeg principaal elke lege vergelijking."""
    with pytest.raises(ValidationError, match="name"):
        ProjectUser(type=LidSoort.SERVICE_ACCOUNT, role="developer")


def test_project_user_eist_een_e_mailadres_bij_een_mens() -> None:
    with pytest.raises(ValidationError, match="email"):
        ProjectUser(role="admin")
