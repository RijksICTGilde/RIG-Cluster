"""Tests for the code metrics report (`task code-metrics`).

What must stay correct is the counting itself: the rounds compare their numbers against an
earlier run, so a function length, a complexity, a parameter count, a nesting depth and the
three kinds of import have to mean the same thing every time.
"""

from __future__ import annotations

from scripts.measure_code import ImportMetrics, ModuleMetrics, Report, _arrows, measure_module, package_arrows


def metrics_of(source: str, module: str = "opi.example"):
    """The metrics of one module, as the script reads it from a file."""
    return measure_module(source, module, f"{module.replace('.', '/')}.py")


def function_named(source: str, name: str):
    _, visitor = metrics_of(source)
    return next(entry for entry in visitor.functions if entry.qualname == name)


def test_function_length_counts_from_the_def_line() -> None:
    """A decorator is not part of the body a reader has to fit on a screen."""
    source = "@decorator\ndef work():\n    a = 1\n    return a\n"
    assert function_named(source, "work").lines == 3


def test_complexity_counts_one_path_plus_each_decision() -> None:
    source = "def work(x):\n    if x and x > 2:\n        for item in x:\n            pass\n    return x\n"
    assert function_named(source, "work").complexity == 4


def test_complexity_of_a_nested_function_stays_with_that_function() -> None:
    source = (
        "def outer(x):\n    def inner(y):\n        if y:\n            return 1\n        return 2\n    return inner\n"
    )
    assert function_named(source, "outer").complexity == 1
    assert function_named(source, "outer.inner").complexity == 2


def test_parameters_ignore_self_and_cls() -> None:
    source = "class Thing:\n    def work(self, a, b):\n        pass\n\n    @classmethod\n    def make(cls, a):\n        pass\n"
    assert function_named(source, "Thing.work").parameters == 2
    assert function_named(source, "Thing.make").parameters == 1


def test_parameters_count_star_args_and_keywords() -> None:
    source = "def work(a, *rest, b=1, **extra):\n    pass\n"
    assert function_named(source, "work").parameters == 4


def test_depth_counts_nested_blocks_and_not_plain_statements() -> None:
    source = (
        "def work(x):\n"
        "    value = 1\n"
        "    with open(x) as handle:\n"
        "        for line in handle:\n"
        "            if line:\n"
        "                value += 1\n"
        "    return value\n"
    )
    assert function_named(source, "work").depth == 3


def test_an_elif_chain_stays_on_one_level() -> None:
    """The parser nests an ``elif`` in the ``orelse`` of the one before it."""
    source = "def work(x):\n    if x == 1:\n        return 1\n    elif x == 2:\n        return 2\n    elif x == 3:\n        return 3\n    return 0\n"
    entry = function_named(source, "work")
    assert entry.depth == 1
    assert entry.complexity == 4


def test_a_guard_clause_stays_one_level_deep() -> None:
    source = "def work(x):\n    if not x:\n        return None\n    return x\n"
    assert function_named(source, "work").depth == 1


def test_the_three_kinds_of_import_are_counted_apart() -> None:
    source = (
        "from typing import TYPE_CHECKING\n"
        "\n"
        "from opi.core.config import settings\n"
        "\n"
        "if TYPE_CHECKING:\n"
        "    from opi.services.services import ServiceAdapter\n"
        "\n"
        "def work():\n"
        "    from opi.manager.project_manager import ProjectManager\n"
        "    return ProjectManager\n"
    )
    _, visitor = metrics_of(source)
    kinds = {entry.target: entry.kind for entry in visitor.imports}
    assert kinds["opi.core.config"] == "top_level"
    assert kinds["opi.services.services"] == "type_checking"
    assert kinds["opi.manager.project_manager"] == "in_function"


def test_a_relative_import_in_a_module_starts_at_its_package() -> None:
    _, visitor = metrics_of("from .rules import normalize_image\n", module="opi.services.catalog.image_registries.web")
    assert visitor.imports[0].target == "opi.services.catalog.image_registries.rules"


def test_a_relative_import_in_an_init_starts_at_the_package_itself() -> None:
    _, visitor = measure_module("from .layout import Div\n", "opi.forms", "opi/forms/__init__.py")
    assert visitor.imports[0].target == "opi.forms.layout"


def test_module_counts_its_own_classes_and_functions() -> None:
    source = "def work():\n    pass\n\nclass Thing:\n    def do(self):\n        pass\n"
    module, _ = metrics_of(source)
    assert (module.classes, module.functions, module.lines) == (1, 2, 6)


def test_a_class_counts_only_its_own_methods() -> None:
    source = "class Thing:\n    def one(self):\n        def helper():\n            pass\n\n    def two(self):\n        pass\n"
    _, visitor = metrics_of(source)
    assert [(entry.name, entry.methods) for entry in visitor.classes] == [("Thing", 2)]


def test_arrows_skip_imports_inside_the_same_layer_package() -> None:
    modules = [
        ModuleMetrics("opi.forms.renderer", "opi/forms/renderer.py", 1, 0, 0),
        ModuleMetrics("opi.forms.layout", "opi/forms/layout.py", 1, 0, 0),
        ModuleMetrics("opi.core.config", "opi/core/config.py", 1, 0, 0),
    ]
    imports = [
        ImportMetrics("opi.forms.renderer", "opi.forms.layout", "top_level", 1),
        ImportMetrics("opi.forms.renderer", "opi.core.config", "top_level", 2),
        ImportMetrics("opi.forms.renderer", "logging", "top_level", 3),
    ]
    assert package_arrows(imports, modules, "opi") == {("opi.forms", "opi.core", "top_level"): 1}


def test_arrows_count_each_kind_on_its_own() -> None:
    modules = [
        ModuleMetrics("opi.web.router", "opi/web/router.py", 1, 0, 0),
        ModuleMetrics("opi.core.config", "opi/core/config.py", 1, 0, 0),
    ]
    imports = [
        ImportMetrics("opi.web.router", "opi.core.config", "top_level", 1),
        ImportMetrics("opi.web.router", "opi.core.config", "in_function", 2),
        ImportMetrics("opi.web.router", "opi.core.config", "in_function", 3),
    ]
    assert package_arrows(imports, modules, "opi") == {
        ("opi.web", "opi.core", "top_level"): 1,
        ("opi.web", "opi.core", "in_function"): 2,
    }


def test_the_pair_label_counts_distinct_pairs_not_one_per_kind() -> None:
    """Two kinds on the same pair are one pair, not two: the label said ``len(arrows)``,
    which counts a (source, target, kind) entry per kind, so one pair reached by both a
    top-level and an in-function import read as two."""
    report = Report(
        modules=[
            ModuleMetrics("opi.web.router", "opi/web/router.py", 1, 0, 0),
            ModuleMetrics("opi.core.config", "opi/core/config.py", 1, 0, 0),
        ],
        functions=[],
        classes=[],
        imports=[
            ImportMetrics("opi.web.router", "opi.core.config", "top_level", 1),
            ImportMetrics("opi.web.router", "opi.core.config", "in_function", 2),
        ],
    )
    assert "1 paren, 2 imports" in _arrows(report, "opi")[1]
