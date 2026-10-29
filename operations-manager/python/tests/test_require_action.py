"""``require_action`` (opi/web/authz.py): 403 op een te smalle rol, zonder er een
beslissing van te maken.

Voor een GET die alleen een formulier herrendert (``backup_select_deployment``,
``restore_select_mode``): de audit hoort geen rij te krijgen over een back-up of een
herstel dat niet gebeurde, dus deze route weegt met ``may`` en nooit met ``authorize``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from opi.core.authz.actions import Action
from opi.web.authz import TE_SMALLE_ROL, require_action

PROJECT_NAME = "demo"


def _request() -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace())


def test_een_toegelaten_rol_geeft_geen_fout_en_geen_beslissing() -> None:
    with (
        patch("opi.web.authz.may", return_value=True) as gate,
        patch("opi.web.authz.record_decision") as record,
    ):
        require_action(_request(), Action.BACKUP_CREATE, PROJECT_NAME)

    gate.assert_called_once()
    record.assert_not_called()


def test_een_te_smalle_rol_geeft_403_met_de_vaste_tekst() -> None:
    with patch("opi.web.authz.may", return_value=False), pytest.raises(HTTPException) as info:
        require_action(_request(), Action.RESTORE_RUN, PROJECT_NAME)

    assert info.value.status_code == 403
    assert info.value.detail == TE_SMALLE_ROL
