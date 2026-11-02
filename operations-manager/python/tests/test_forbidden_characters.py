"""De laatste controle op gevaarlijke tekens in een projectbestand (RC-327, stap 1).

``find_forbidden_characters`` is de backstop in ``GitProjectStore._validate``: hij geldt
voor elke stringwaarde, ook velden zonder eigen pattern. Deze tests dekken de functie
zelf; de wiring in de store (enforce=True/False) staat in test_project_store.py.
"""

from __future__ import annotations

from opi.utils.forbidden_characters import find_forbidden_characters


def test_een_schone_waarde_levert_niets_op() -> None:
    assert find_forbidden_characters({"name": "proj", "n": 1, "ok": True, "none": None}) == []


def test_een_nul_byte_in_een_geneste_waarde_wordt_gevonden() -> None:
    violations = find_forbidden_characters({"components": [{"image": "nginx\x00evil"}]})
    assert violations == ["components[0].image (NUL)"]


def test_een_bidi_control_wordt_gevonden() -> None:
    # U+202E: right-to-left override, het Trojan Source-teken. Via chr(), niet als
    # letterlijk teken in de bron: een bidi-control in de broncode zelf is precies
    # wat deze toets vangt.
    rtl_override = chr(0x202E)
    violations = find_forbidden_characters({"deployments": [{"namespace": f"a{rtl_override}b"}]})
    assert violations == ["deployments[0].namespace (U+202E)"]


def test_de_waarde_zelf_komt_niet_in_de_melding() -> None:
    violations = find_forbidden_characters({"config": {"api-key": "plain-secret-\x00-waarde"}})
    assert violations == ["config.api-key (NUL)"]
    for v in violations:
        assert "plain-secret" not in v


def test_een_sleutel_met_een_bidi_teken_wordt_ook_gevonden() -> None:
    """Het pad noemt de sleutel, maar met het bidi-teken als codepunt: een melding die
    hem letterlijk weergeeft toont de herordening die deze toets moet voorkomen."""
    key = f"a{chr(0x200E)}b"
    violations = find_forbidden_characters({"users": [{key: "ok"}]})
    assert violations == ["users[0].aU+200Eb (U+200E)"]
    assert chr(0x200E) not in violations[0]


def test_tab_en_newline_zijn_toegestaan() -> None:
    # heredoc-scripts (command) gebruiken tabs en newlines; die mogen niet geweigerd worden.
    assert find_forbidden_characters({"command": "line1\n\tline2"}) == []


def test_carriage_return_wordt_bij_naam_gemeld() -> None:
    violations = find_forbidden_characters({"env-vars": {"FOO": "a\rb"}})
    assert violations == ["env-vars.FOO (CR)"]


def test_meerdere_schendingen_komen_allemaal_terug() -> None:
    violations = find_forbidden_characters({"a": "x\x00", "b": ["ok", "y\x00"]})
    assert violations == ["a (NUL)", "b[1] (NUL)"]


def test_de_wortel_zonder_pad_heet_wortel() -> None:
    assert find_forbidden_characters("x\x00") == ["(wortel) (NUL)"]
