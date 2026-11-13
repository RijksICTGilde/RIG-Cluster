"""``registry.collect_service_routers()``: a service that owns a page block owns its routes.

Several services hand back the SAME router object -- every backupable service returns
``backups_router``, and both PostgreSQL services return ``db_console_router`` and
``jobs_router`` via ``DatabasePagesMixin``. The collector must keep each one exactly
once: without identity deduplication, a shared router mounts once per owner and
``web_router`` ends up serving the same route under duplicate route entries.
"""

from __future__ import annotations

from opi.services.catalog.shared.backups import backups_router
from opi.services.catalog.shared.db_console import db_console_router
from opi.services.catalog.shared.jobs import jobs_router
from opi.services.registry import collect_service_routers


def test_a_router_shared_by_several_services_is_collected_once() -> None:
    routers = collect_service_routers()

    assert routers.count(backups_router) == 1
    assert routers.count(db_console_router) == 1
    assert routers.count(jobs_router) == 1
    assert len(routers) == len({id(router) for router in routers}), "every collected router must be a distinct object"
