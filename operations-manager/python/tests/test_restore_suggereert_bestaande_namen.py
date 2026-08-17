"""De 404 bij een restore noemt namen die ook echt bestaan (zad-cli, punt 10b).

DE MELDING

``zad backup list`` gaf ``productie-postgresql``. Wie een verkeerde naam opgaf, kreeg een
404 die ``productie-database`` suggereerde -- en die naam draagt geen enkele snapshot. De
suggestie stuurde dus precies de verkeerde kant op, en dat op het moment dat iemand al
verdwaald is.

Erger dan de suggestie was dat die naam ook geaccepteerd werd: de restore startte en
strandde pas daarna op een snapshot die niet bestaat.

DE OORZAAK

Twee naamconventies voor dezelfde naam. De schrijver leidt af als ``{deployment}-{dienstnaam
tot het eerste streepje}``, dus ``postgresql-database`` wordt ``productie-postgresql``. Het
restorepad gebruikte een vast achtervoegsel: ``{deployment}-database``. Bij buckets viel dat
toevallig samen (``minio-storage`` begint ook met ``minio``), bij databases niet -- en
daarom is dit twee jaar niemand opgevallen.

Deze test legt de regel van de SCHRIJVER vast als de regel die beide kanten volgen.
"""

from __future__ import annotations

from opi.api.restore_router import _fallback_reference_names

DATABASE_DIENSTEN = ["postgresql-database", "namespace-postgresql-database"]
BUCKET_DIENSTEN = ["minio-storage"]


def test_een_database_heet_naar_zijn_dienst_en_niet_database() -> None:
    """De naam die de backup werkelijk draagt, en die backup list ook toont."""
    namen = _fallback_reference_names("productie", DATABASE_DIENSTEN, "database")

    assert "productie-postgresql" in namen
    assert "productie-namespace" in namen


def test_de_oude_naam_blijft_werken() -> None:
    """Wat er ooit onder de oude naam is weggeschreven moet terug te zetten blijven.

    Vandaar dat het vaste achtervoegsel erbij blijft staan in plaats van vervangen te
    worden: een reparatie die bestaande backups onbereikbaar maakt is geen reparatie.
    """
    assert "productie-database" in _fallback_reference_names("productie", DATABASE_DIENSTEN, "database")


def test_bij_buckets_verandert_er_niets() -> None:
    """Daar vielen de twee conventies al samen; dat mag niet ineens twee namen worden."""
    namen = _fallback_reference_names("productie", BUCKET_DIENSTEN, "minio")

    assert namen == ["productie-minio"]


def test_geen_dubbele_namen() -> None:
    """Dezelfde naam twee keer in een 404 leest als twee mogelijkheden."""
    namen = _fallback_reference_names("productie", DATABASE_DIENSTEN, "postgresql")

    assert len(namen) == len(set(namen))
