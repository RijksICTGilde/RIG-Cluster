"""De baseline: wat een reden wel en niet dekt, en op welk doel.

De kolom ``doel`` is de plek waar een reden stil te ruim kan worden: een uitleg die alleen in
de wegwerp-sandbox geldt zou tegen een cluster-sandbox een echte bevinding wegnemen.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from security_tests.baseline import BASELINE, lees_baseline, toets_tegen_baseline
from security_tests.doel import LOKAAL, SANDBOX

if TYPE_CHECKING:
    from pathlib import Path

KOPREGEL = "proef\tdoel\tkenmerk\treden\tbron"


def _baseline(tmp_path: Path, *rijen: str) -> Path:
    pad = tmp_path / "bekende-bevindingen.tsv"
    pad.write_text("\n".join([KOPREGEL, *rijen]) + "\n")
    return pad


def test_een_regel_zonder_doel_geldt_op_elk_doel(tmp_path: Path) -> None:
    pad = _baseline(tmp_path, "web-payloads\t\tGET /x\tvals positief\tREADME.md")
    assert lees_baseline("web-payloads", LOKAAL, pad) == {"GET /x": "vals positief"}
    assert lees_baseline("web-payloads", SANDBOX, pad) == {"GET /x": "vals positief"}


def test_een_regel_op_lokaal_dekt_de_cluster_sandbox_niet(tmp_path: Path) -> None:
    """Faalconditie: "draait niet in de wegwerp-sandbox" neemt ook in het cluster een bevinding weg."""
    pad = _baseline(tmp_path, f"web-payloads\t{LOKAAL}\tGET /x\tgeen database in de wegwerp-sandbox\tREADME.md")
    assert lees_baseline("web-payloads", LOKAAL, pad) == {"GET /x": "geen database in de wegwerp-sandbox"}
    assert lees_baseline("web-payloads", SANDBOX, pad) == {}


def test_zonder_doelsoort_komt_de_hele_lijst_terug(tmp_path: Path) -> None:
    pad = _baseline(
        tmp_path,
        f"web-payloads\t{LOKAAL}\tGET /x\talleen lokaal\tREADME.md",
        "web-payloads\t\tGET /y\telk doel\tREADME.md",
    )
    assert set(lees_baseline("web-payloads", pad=pad)) == {"GET /x", "GET /y"}


def test_de_ingecheckte_baseline_dekt_de_wegwerp_sandbox_alleen_op_lokaal() -> None:
    """Een reden die de wegwerp-sandbox noemt mag tegen de cluster-sandbox niets dekken."""
    for proef in ("web-payloads", "zap-ongeauthenticeerd", "zap-projectlid", "zap-zad-admin"):
        voor_sandbox = lees_baseline(proef, SANDBOX, BASELINE)
        assert not [reden for reden in voor_sandbox.values() if "wegwerp-sandbox" in reden], (
            f"{proef}: een reden over de wegwerp-sandbox dekt ook het cluster-doel"
        )


def test_een_bevinding_zonder_uitleg_op_dit_doel_blijft_een_melding(tmp_path: Path) -> None:
    """Dezelfde bevinding is lokaal een afwijking met uitleg en op de cluster-sandbox nieuw."""
    pad = _baseline(tmp_path, f"web-payloads\t{LOKAAL}\tGET /x\talleen lokaal\tREADME.md")
    assert toets_tegen_baseline("web-payloads", {"GET /x": "500"}, LOKAAL, pad) == []
    assert toets_tegen_baseline("web-payloads", {"GET /x": "500"}, SANDBOX, pad) == ["NIEUW GET /x: 500"]
