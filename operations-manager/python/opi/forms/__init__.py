"""Schema driven form generation for OPI.

Import a part from the module it lives in: ``FormMeta`` from ``opi.forms.schema``, the
layout elements from ``opi.forms.layout``, ``FormRenderer`` from ``opi.forms.renderer``, and
the converters, hooks, widgets, models and visualizers from the module of that name.

This package re-exports nothing. Although docs/development/coding-rules.md says packages
should expose their interface in ``__init__``, doing so here creates a circular import:
``opi.forms.visualizers.providers`` loads ``opi.services.catalog.image_registries``, which
imports ``opi.forms`` back. Without the re-export, each import succeeds. With it, the entire
opi codebase loads on the first import of any ``opi.forms`` submodule.
"""
