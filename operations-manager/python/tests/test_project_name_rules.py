"""Project name acceptance by each guard, compared side by side."""

import json
import re
from pathlib import Path
from unittest.mock import MagicMock

import jsonschema
import pytest
from opi.core.config import settings
from opi.forms.editables.generators import ProjectNameGenerator
from opi.forms.models.project_file import ProjectFileModel
from opi.utils.naming import REGISTRY_TAG_OWNER_RE
from opi.utils.patterns import PROJECT_NAME
from opi.utils.project_names import (
    ensure_unique_project_name,
    generate_project_name,
    validate_project_name,
)
from pydantic import ValidationError, create_model

SCHEMA_PATH = Path(__file__).parent.parent / "opi" / "schemas" / "project_v2.json"


def _schema_accepts(name: str) -> bool:
    name_schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))["properties"]["name"]
    return jsonschema.Draft202012Validator(name_schema).is_valid(name)


def _form_model_accepts(name: str) -> bool:
    """Only the ``name`` field of ``ProjectFileModel``, with the rule that field carries."""
    one_field = create_model("OneField", name=(str, ProjectFileModel.model_fields["name"]))
    try:
        one_field(name=name)
    except ValidationError:
        return False
    return True


def _registry_tag_accepts(name: str) -> bool:
    """The owner prefix rule of a registry tag, as ``SkopeoConnector`` applies it."""
    return bool(name) and REGISTRY_TAG_OWNER_RE.match(name) is not None


# name, validate_project_name, registry tag owner, JSON schema, form model
NAME_CASES: list[tuple[str, bool, bool, bool, bool]] = [
    ("a" * 20, True, True, True, True),
    ("a" * 21, False, True, True, True),
    ("a" * 30, False, True, True, True),
    ("a" * 31, False, True, False, True),
    ("abc-", True, True, True, True),
    ("a--b", True, True, True, True),
    ("Abc", False, False, False, False),
    ("1abc", False, False, False, False),
    ("a_b", False, False, False, False),
    ("-abc", False, False, False, False),
    ("ab c", False, False, False, False),
    ("", False, False, False, False),
]


@pytest.mark.parametrize(
    ("name", "by_validate_project_name", "by_registry_tag", "by_schema", "by_form"),
    [pytest.param(*case, id=repr(case[0])) for case in NAME_CASES],
)
def test_every_guard_on_the_same_name(
    name: str,
    by_validate_project_name: bool,
    by_registry_tag: bool,
    by_schema: bool,
    by_form: bool,
) -> None:
    assert validate_project_name(name) is by_validate_project_name
    assert _registry_tag_accepts(name) is by_registry_tag
    assert _schema_accepts(name) is by_schema
    assert _form_model_accepts(name) is by_form


def test_no_guard_lets_a_trailing_newline_through() -> None:
    """``$`` matches before a final newline in ``re``, so the anchor has to be ``\\Z``."""
    assert validate_project_name("abc\n") is False
    assert _registry_tag_accepts("abc\n") is False
    assert _schema_accepts("abc\n") is False
    assert _form_model_accepts("abc\n") is False


def test_both_spellings_of_the_pattern_judge_the_same_names() -> None:
    """The two spellings may never disagree about a name.

    They differ only in the anchor their regex engine accepts.
    """
    names = [case[0] for case in NAME_CASES] + ["abc\n", "abc"]
    for name in names:
        by_re = re.match(PROJECT_NAME, name) is not None and bool(name)
        assert by_re is _form_model_accepts(name), name


def test_a_name_of_25_is_storable_but_not_manageable() -> None:
    """The two length limits disagree, so git can bring in a project that every route and
    task afterwards refuses.
    """
    name = "a" * 25
    assert _schema_accepts(name) is True
    assert validate_project_name(name) is False


@pytest.mark.parametrize(
    "display_name",
    [
        "My New Application",
        "WebShop",
        "API Gateway Service",
        "a",
        "Project 2026",
        "!!! Zeer Lange Weergavenaam Met Veel Woorden !!!",
        "een-naam-met-streepjes",
    ],
)
def test_a_generated_name_passes_the_validator(display_name: str) -> None:
    name, _display = generate_project_name(display_name)
    assert validate_project_name(name) is True


def test_a_generated_name_passes_the_validator_with_a_fixed_postfix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "FIXED_PROJECT_POSTFIX", "zzz")
    name, _display = generate_project_name("My New Application")
    assert name == "mna-zzz"
    assert validate_project_name(name) is True


def test_a_unique_name_falls_back_to_numbering_with_a_fixed_postfix(monkeypatch: pytest.MonkeyPatch) -> None:
    """With a fixed postfix every regeneration gives the same name, so only the numbered
    fallback can still produce a free one.
    """
    monkeypatch.setattr(settings, "FIXED_PROJECT_POSTFIX", "zzz")
    name, _display = ensure_unique_project_name("My New Application", {"mna-zzz"})
    assert name != "mna-zzz"
    assert re.match(r"^mna-zz\d$", name)
    assert validate_project_name(name) is True


def test_the_wizard_generator_avoids_a_name_already_in_the_store(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "FIXED_PROJECT_POSTFIX", "zzz")
    existing = MagicMock()
    existing.name = "mna-zzz"
    store = MagicMock()
    store.get_all = MagicMock(return_value=[existing])
    monkeypatch.setattr("opi.forms.editables.generators.get_project_store", lambda: store)

    name = ProjectNameGenerator().generate({"display-name": "My New Application"})

    assert name != "mna-zzz"
    assert validate_project_name(name) is True
