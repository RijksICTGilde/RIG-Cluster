"""De migratieketen heeft precies een head.

Twee takken die elk een migratie op dezelfde voorganger zetten mergen zonder conflict:
git ziet twee nieuwe bestanden en geen overlap. Alembic houdt er dan twee heads over en
``alembic upgrade head`` weigert met "Multiple head revisions are present". Die fout komt
pas bij het opstarten van de pod, en dus na de merge. Deze toets haalt hem naar voren.

Loopt hij rood, dan hangt de nieuwste migratie aan de verkeerde voorganger: zet haar
``down_revision`` op de andere head.
"""

from __future__ import annotations

from tests.conftest import alembic_script_directory


def test_de_keten_heeft_een_enkele_head() -> None:
    heads = alembic_script_directory().get_heads()
    assert len(heads) == 1, f"meer dan een alembic-head: {sorted(heads)}"
