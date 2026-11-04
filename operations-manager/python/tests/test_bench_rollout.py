"""Tests voor de rekenlogica van de rollout-bank (RC-418).

De bank praat met een echte sandbox, maar wat ze daarmee doet (de Prometheus-tekst
parsen, het verschil tussen twee scrapes nemen, de ZAD-tijd afleiden, de tabel
opbouwen) is pure logica die hier zonder sandbox te toetsen is. Dat is ook precies het
deel dat een plausibel verkeerd getal zou opleveren zonder dat iemand het ziet.
"""

from __future__ import annotations

from scripts.bench_rollout import FaseMeting, Uitkomst, _diff, _gepinde_verwijzing, _label, _tabel, _task_id
from scripts.bench_rollout import main as bench_main


def test_label_haalt_de_waarde_uit_een_prometheus_labelset() -> None:
    labels = 'task_type="upsert_deployment",phase="argo_wait_synced"'
    assert _label(labels, "task_type") == "upsert_deployment"
    assert _label(labels, "phase") == "argo_wait_synced"


def test_label_geeft_leeg_voor_een_ontbrekend_label() -> None:
    assert _label('phase="generate"', "task_type") == ""


class TestDiff:
    def test_een_fase_die_in_beide_scrapes_voorkomt_geeft_het_verschil(self) -> None:
        voor = {"upsert_deployment": FaseMeting(som={"generate": 10.0}, aantal={"generate": 5.0})}
        na = {"upsert_deployment": FaseMeting(som={"generate": 13.5}, aantal={"generate": 6.0})}

        uitkomst = _diff(voor, na, "upsert_deployment")

        assert uitkomst.som["generate"] == 3.5
        assert uitkomst.aantal["generate"] == 1.0

    def test_een_fase_zonder_nieuwe_observaties_valt_weg(self) -> None:
        """Zonder dit zou een ongebruikte fase als 0,0 s in het verslag staan."""
        voor = {"t": FaseMeting(som={"generate": 10.0}, aantal={"generate": 5.0})}
        na = {"t": FaseMeting(som={"generate": 10.0}, aantal={"generate": 5.0})}

        uitkomst = _diff(voor, na, "t")

        assert "generate" not in uitkomst.som
        assert "generate" not in uitkomst.aantal

    def test_een_taaktype_dat_voor_de_meting_nog_niet_bestond_telt_vanaf_nul(self) -> None:
        na = {"nieuw": FaseMeting(som={"generate": 2.0}, aantal={"generate": 1.0})}

        uitkomst = _diff({}, na, "nieuw")

        assert uitkomst.som["generate"] == 2.0


def test_wachttijd_telt_alleen_de_argocd_en_pod_fases() -> None:
    meting = FaseMeting(som={"git_prepare": 9.0, "argo_wait_synced": 4.0, "argo_wait_healthy": 1.5})
    assert meting.wachttijd() == 5.5


def test_zad_tijd_is_de_taakduur_min_de_wacht() -> None:
    uitkomst = Uitkomst(
        "a1",
        "upsert_deployment",
        "completed",
        42.0,
        FaseMeting(som={"argo_wait_synced": 4.0, "argo_wait_healthy": 1.0}),
    )
    assert uitkomst.zad_tijd == 37.0


def test_zad_tijd_gaat_nooit_onder_nul_als_de_wacht_de_taakduur_overschrijdt() -> None:
    """Metingen op twee verschillende scrapes kunnen door afronding net over elkaar vallen."""
    uitkomst = Uitkomst("a1", "upsert_deployment", "completed", 3.0, FaseMeting(som={"argo_wait_synced": 5.0}))
    assert uitkomst.zad_tijd == 0.0


def test_task_id_leest_zowel_het_platte_als_het_geneste_veld() -> None:
    assert _task_id('{"task_id": "abc"}') == "abc"
    assert _task_id('{"task": {"task_id": "def"}}') == "def"


def test_task_id_zonder_task_id_stopt_met_een_duidelijke_melding() -> None:
    import pytest

    with pytest.raises(SystemExit, match="geen task_id"):
        _task_id("{}")


class TestGepindeVerwijzing:
    def test_een_image_zonder_digest_wordt_overgeslagen_als_de_registry_niet_ghcr_is(self) -> None:
        assert _gepinde_verwijzing("docker.io/library/nginx:1.27") == ""

    def test_een_image_dat_al_een_digest_draagt_levert_niets_op(self) -> None:
        assert _gepinde_verwijzing("ghcr.io/org/app@sha256:" + "a" * 64) == ""

    def test_een_image_zonder_tag_levert_niets_op(self) -> None:
        assert _gepinde_verwijzing("ghcr.io/org/app") == ""


def test_tabel_bevat_elke_gevraagde_fase_per_scenario() -> None:
    uitkomst = Uitkomst(
        "a1", "upsert_deployment", "completed", 12.3, FaseMeting(som={"generate": 1.0}, aantal={"generate": 1.0})
    )
    tekst = _tabel([uitkomst])
    assert "| a1 | upsert_deployment | completed | 12.3 s |" in tekst
    assert "| a1 | generate | 1 | 1.0 s |" in tekst


def test_tabel_zonder_uitkomsten_blijft_alleen_de_koppen() -> None:
    tekst = _tabel([])
    assert "scenario" in tekst
    assert "| a1" not in tekst


def test_main_zonder_project_argument_geeft_een_gebruiksfout() -> None:
    import pytest

    with pytest.raises(SystemExit):
        bench_main(["bench_rollout.py"])
