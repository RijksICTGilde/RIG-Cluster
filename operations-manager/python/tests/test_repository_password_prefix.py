"""Een repo-wachtwoord zonder prefix hoort niet in een projectbestand te belanden.

Het schema eist voor ``repositories[].password`` een AGE-blok of een expliciete
``plain:``. Toen fundament-poc ``PROJECT_REPO_PASSWORD`` via een secretKeyRef uit het
Secret ``forgejo-admin`` ging halen, kwam de waarde kaal binnen en schreef
``generate_project_yaml`` hem ongewijzigd weg. Elke projectaanmaak strandde daarna op
"Veld 'repositories/0/password' voldoet niet aan het projectschema: de waarde is
afgekeurd door schemaregel 'oneOf'".

De reparatie zit in ``project_utils``: een waarde zonder prefix wordt met de AGE-sleutel
van de operations-manager versleuteld voordat hij het bestand in gaat. Zo staat het
wachtwoord op een plek (het Secret, dat ArgoCD en de init-Job ook lezen) en komt er geen
platte tekst in git.

Rood (kale waarde doorgeschreven): de eerste test faalt op de schemavalidatie.
Groen: alleen de drie vormen die hun opslag benoemen komen erdoor.
"""

import pytest
from opi.core.project_schema import ProjectSchemaError, validate_project_schema
from opi.utils.age import has_password_prefix

_AGE_BLOK = "-----BEGIN AGE ENCRYPTED FILE-----\nYWdlLWVuY3J5cHRpb24ub3JnL3Yx\n-----END AGE ENCRYPTED FILE-----\n"


def _project(password: str) -> dict:
    return {
        "schema-version": 2.8,
        "name": "voorbeeld",
        "clusters": ["fundament-poc"],
        "repositories": [
            {
                "name": "main-repo",
                "url": "http://forgejo.rig-system.svc.cluster.local:3000/rig-admin/zad-deployments.git",
                "username": "rig-admin",
                "password": password,
                "branch": "main",
                "path": ".",
            }
        ],
    }


def test_kaal_wachtwoord_wordt_geweigerd() -> None:
    """Precies de melding uit het portaal: dit mag niet stilzwijgend goed gaan."""
    with pytest.raises(ProjectSchemaError, match="repositories/0/password"):
        validate_project_schema(_project("CArczGkFfMyUB9nKCVYsipSw"))


@pytest.mark.parametrize(
    "password",
    [
        "base64+age:LS0tLS1CRUdJTiBBR0UgRU5DUllQVEVEIEZJTEUtLS0tLQo=",
        "plain:admin1234",
        _AGE_BLOK,
    ],
)
def test_benoemde_opslagvormen_komen_erdoor(password: str) -> None:
    validate_project_schema(_project(password))


@pytest.mark.parametrize(
    ("password", "verwacht"),
    [
        ("base64+age:LS0tLS1CRUdJTg==", True),
        ("age:LS0tLS1CRUdJTg==", True),
        ("plain:admin1234", True),
        (_AGE_BLOK, True),
        # Kaal uit een Kubernetes Secret: dit is de enige vorm die nog versleuteld moet.
        ("CArczGkFfMyUB9nKCVYsipSw", False),
        ("", False),
    ],
)
def test_has_password_prefix(password: str, verwacht: bool) -> None:
    assert has_password_prefix(password) is verwacht
