"""``authorized_project`` (opi/web/authz.py): een handler leest het project uit de
beslissing die ``enforce_route_action`` al nam, in plaats van ``authorize`` nog eens
aan te roepen.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from opi.web.authz import authorized_project


def test_geeft_het_project_terug_dat_de_poort_al_toeliet() -> None:
    project = SimpleNamespace(name="demo")
    request = SimpleNamespace(state=SimpleNamespace(authz_project=project))
    assert authorized_project(request) is project


def test_zonder_een_gedraaide_poort_is_het_een_serverfout_geen_403() -> None:
    """Een 403 zou hier net zo goed een ontbrekende dependency-koppeling kunnen
    maskeren als een echte weigering: dat zijn twee verschillende problemen.
    """
    request = SimpleNamespace(state=SimpleNamespace())
    with pytest.raises(HTTPException) as info:
        authorized_project(request)
    assert info.value.status_code == 500
