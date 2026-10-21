"""Validatie van de flowmatrix-YAML in `matrix.load_matrix`.

Dit bestand groeit met handmatige edits (zie flows_odcn.yaml), dus een typo in
`verwacht` of een verwijzing naar een endpoint dat niet bestaat moet een duidelijke
fout geven in plaats van stil de verkeerde kant op te evalueren.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from tests.netpol.matrix import load_matrix

if TYPE_CHECKING:
    from pathlib import Path


def _write(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "flows.yaml"
    path.write_text(content)
    return path


def test_een_onbekende_verwacht_waarde_geeft_een_duidelijke_fout(tmp_path):
    path = _write(
        tmp_path,
        """
endpoints:
  a: {namespace: ns}
  b: {namespace: ns}
flows:
  - {van: a, naar: b, poort: 80, verwacht: misschien}
""",
    )
    with pytest.raises(ValueError, match="open of dicht"):
        load_matrix(path)


def test_een_onbekend_endpoint_in_een_flow_geeft_een_duidelijke_fout(tmp_path):
    path = _write(
        tmp_path,
        """
endpoints:
  a: {namespace: ns}
flows:
  - {van: a, naar: niet-bestaand, poort: 80, verwacht: open}
""",
    )
    with pytest.raises(ValueError, match="onbekend endpoint"):
        load_matrix(path)


def test_zonder_allow_all_valt_terug_op_verwacht_als_hij_ontbreekt(tmp_path):
    path = _write(
        tmp_path,
        """
endpoints:
  a: {namespace: ns}
  b: {namespace: ns}
flows:
  - {van: a, naar: b, poort: 80, verwacht: dicht}
""",
    )
    _, flows = load_matrix(path)
    assert flows[0].zonder_allow_all == "dicht"


def test_zonder_allow_all_met_een_eigen_waarde_overschrijft_verwacht(tmp_path):
    path = _write(
        tmp_path,
        """
endpoints:
  a: {namespace: ns}
  b: {namespace: ns}
flows:
  - {van: a, naar: b, poort: 80, verwacht: open, zonder_allow_all: dicht}
""",
    )
    _, flows = load_matrix(path)
    assert flows[0].zonder_allow_all == "dicht"


def test_een_onbekende_zonder_allow_all_waarde_geeft_een_duidelijke_fout(tmp_path):
    path = _write(
        tmp_path,
        """
endpoints:
  a: {namespace: ns}
  b: {namespace: ns}
flows:
  - {van: a, naar: b, poort: 80, verwacht: open, zonder_allow_all: misschien}
""",
    )
    with pytest.raises(ValueError, match="open of dicht"):
        load_matrix(path)
