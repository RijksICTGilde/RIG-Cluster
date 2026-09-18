"""De suffixen van de versleutelpijplijn horen bij een eigenaar.

De twee namen die een secret draagt stonden op zeventien plekken met de hand gebouwd of
met ``replace()`` omgezet. Deze toetsen pinnen de eigenaar.
"""

import ast
import pathlib

import opi
from opi.utils.sops import SOPS_SUFFIX, TO_SOPS_SUFFIX, sops_filenames


class TestSuffixen:
    def test_waarden_liggen_vast(self) -> None:
        """De namen staan in het GitOps-contract; een hernoeming breekt de CMP-plugin."""
        assert TO_SOPS_SUFFIX == ".to-sops.yaml"
        assert SOPS_SUFFIX == ".sops.yaml"


class TestSopsFilenames:
    def test_kale_naam_geeft_beide_namen(self) -> None:
        namen = sops_filenames("demo-secret")
        assert namen.plaintext == "demo-secret.to-sops.yaml"
        assert namen.encrypted == "demo-secret.sops.yaml"

    def test_is_een_tuple_van_plat_dan_versleuteld(self) -> None:
        assert tuple(sops_filenames("demo-secret")) == ("demo-secret.to-sops.yaml", "demo-secret.sops.yaml")

    def test_platte_naam_wordt_versleutelde_naam(self) -> None:
        assert sops_filenames("demo-secret.to-sops.yaml").encrypted == "demo-secret.sops.yaml"

    def test_versleutelde_naam_wordt_platte_naam(self) -> None:
        assert sops_filenames("demo-secret.sops.yaml").plaintext == "demo-secret.to-sops.yaml"

    def test_naam_die_de_suffix_al_draagt_verandert_niet(self) -> None:
        """Idempotent, want beide helften van een paar komen door dezelfde aanroep."""
        assert sops_filenames("demo-secret.to-sops.yaml").plaintext == "demo-secret.to-sops.yaml"
        assert sops_filenames("demo-secret.sops.yaml").encrypted == "demo-secret.sops.yaml"

    def test_pad_blijft_heel(self) -> None:
        """Aanroepers geven relatieve paden door, niet alleen basenamen."""
        assert sops_filenames("odcn/demo/values.to-sops.yaml").encrypted == "odcn/demo/values.sops.yaml"

    def test_punten_in_de_naam_blijven_staan(self) -> None:
        assert sops_filenames("registry.example.com-secret").encrypted == "registry.example.com-secret.sops.yaml"

    def test_gewone_yaml_is_geen_helft_van_het_paar(self) -> None:
        assert sops_filenames("issuer.yaml").plaintext == "issuer.yaml.to-sops.yaml"


_OPI_ROOT = pathlib.Path(opi.__file__).parent
_EIGENAAR = _OPI_ROOT / "utils" / "sops.py"


def _docstrings(boom: ast.AST) -> set[int]:
    """De id's van de tekstknopen die een docstring zijn, zodat de scan ze overslaat."""
    gevonden = set()
    for knoop in ast.walk(boom):
        if not isinstance(knoop, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        eerste = knoop.body[0] if knoop.body else None
        if (
            isinstance(eerste, ast.Expr)
            and isinstance(eerste.value, ast.Constant)
            and isinstance(eerste.value.value, str)
        ):
            gevonden.add(id(eerste.value))
    return gevonden


def _handgemaakte_namen(bron: str, herkomst: str) -> list[str]:
    """Tekstwaarden die op een suffix EINDIGEN, dus een naam of globpatroon.

    Een logregel die de suffix midden in een zin noemt is prose, geen tweede bron.
    """
    boom = ast.parse(bron)
    overslaan = _docstrings(boom)
    return [
        f"{herkomst}:{knoop.lineno}: {knoop.value!r}"
        for knoop in ast.walk(boom)
        if isinstance(knoop, ast.Constant)
        and isinstance(knoop.value, str)
        and id(knoop) not in overslaan
        and knoop.value.endswith((SOPS_SUFFIX, TO_SOPS_SUFFIX))
    ]


#: Een bron met alle drie de gevallen: een docstring, een logzin, en een echte tweede bron.
_PROEFBRON = '''"""Schrijft de platte helft als <naam>.to-sops.yaml"""

logger.info("klaar met .sops.yaml voor dit component")
pad = f"{naam}.to-sops.yaml"
'''


class TestGeenTweedeBron:
    def test_de_scan_vindt_een_teruggezette_literal(self) -> None:
        """Zonder deze toets is een scan die niets meer vindt niet te onderscheiden van schone code."""
        assert _handgemaakte_namen(_PROEFBRON, "proef.py") == ["proef.py:4: '.to-sops.yaml'"]

    def test_alleen_de_eigenaar_noemt_de_suffixen(self) -> None:
        treffers = [
            regel
            for pad in sorted(_OPI_ROOT.rglob("*.py"))
            if pad != _EIGENAAR
            for regel in _handgemaakte_namen(pad.read_text(), str(pad.relative_to(_OPI_ROOT)))
        ]
        assert not treffers, "Gebruik sops_filenames()/de suffixconstanten uit opi/utils/sops.py:\n" + "\n".join(
            treffers
        )
