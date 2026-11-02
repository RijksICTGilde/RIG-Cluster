"""``_doelwit`` (opi/web/authz.py): de poort leest deployment/component uit de query
alleen als de route zich daarvoor VERKLAART met ``@targets_in_query``.

Op de env-vars-componentroute staat de deployment in de query, en de poort moet die
lezen (RC-398). Maar de poort mag nooit een ANDERE resource wegen dan de handler
gebruikt: een route zonder die verklaring mag een toegevoegde ``?deployment_name=...``
niet oppikken, want dat kan de beslissing verruimen.
"""

from __future__ import annotations

from types import SimpleNamespace

from opi.core.authz.actions import query_targets_of, targets_in_query
from opi.web.authz import _doelwit

VERKLAARD = frozenset({"deployment_name"})


def _request(*, path_params: dict[str, str], query_params: dict[str, str]) -> SimpleNamespace:
    return SimpleNamespace(path_params=path_params, query_params=query_params)


def test_de_padwaarde_gaat_voor_de_query() -> None:
    request = _request(path_params={"deployment_name": "prod"}, query_params={"deployment_name": "acc"})
    assert _doelwit(request, VERKLAARD, "deployment_name") == "prod"


def test_de_query_vult_aan_als_de_route_zich_verklaart() -> None:
    request = _request(path_params={}, query_params={"deployment_name": "prod"})
    assert _doelwit(request, VERKLAARD, "deployment_name") == "prod"


def test_de_query_wordt_genegeerd_zonder_verklaring() -> None:
    """Zonder dit kon een aanroeper een resource aanplakken die de handler niet
    gebruikt, en zo ruimer laten beslissen dan de route bedoelt.
    """
    request = _request(path_params={}, query_params={"deployment_name": "prod"})
    assert _doelwit(request, frozenset(), "deployment_name") is None


def test_niets_in_pad_of_query_geeft_niets() -> None:
    request = _request(path_params={}, query_params={})
    assert _doelwit(request, VERKLAARD, "deployment_name") is None


def test_een_route_zonder_markering_verklaart_niets() -> None:
    def handler(deployment_name: str | None = None) -> None: ...

    assert query_targets_of(handler) == frozenset()


def test_de_markering_noemt_de_namen() -> None:
    @targets_in_query("deployment_name")
    def handler(deployment_name: str | None = None) -> None: ...

    assert query_targets_of(handler) == frozenset({"deployment_name"})
