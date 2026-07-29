"""Ungranted approvals are reported per deployment by the service that owns them.

The project page asks the catalog (collect_deployment_approval_notices) instead of
reading the domains block itself, so the consequence of a verdict is written where the
knowledge lives: an unapproved domain does not block the deployment, it publishes on the
cluster address (naming.apply_domain_approval_fallback).
"""

from opi.services.approvals import collect_deployment_approval_notices

DEPLOYMENT = {"name": "productie", "base-domain": "robbertuittenbroek.nl", "subdomain": "test2"}


def _project(domain_status: str, subdomain_status: str, **extra) -> dict:
    return {
        "name": "p",
        "services": [
            {
                "name": "publish-on-web",
                "config": {
                    "domains": {
                        "allowed-domains": [
                            {
                                "domain": "robbertuittenbroek.nl",
                                "status": domain_status,
                                "history": [{"date": "2026-07-27T14:26:38+00:00", "status": domain_status}],
                            }
                        ],
                        "allowed-subdomains": [
                            {
                                "domain": "robbertuittenbroek.nl",
                                "subdomains": [
                                    {
                                        "name": "test2",
                                        "status": subdomain_status,
                                        "history": [
                                            {
                                                "date": "2026-07-27T16:27:43+00:00",
                                                "status": subdomain_status,
                                                "by": "admin@sandbox.rijksapp.dev",
                                                **extra,
                                            }
                                        ],
                                    }
                                ],
                            }
                        ],
                    }
                },
            }
        ],
        "deployments": [DEPLOYMENT],
    }


def test_approved_domain_and_subdomain_report_nothing():
    assert collect_deployment_approval_notices(_project("approved", "approved"), DEPLOYMENT) == []


def test_denied_subdomain_reports_the_verdict_and_the_consequence():
    notices = collect_deployment_approval_notices(
        _project("approved", "denied", message="niet van dit project"), DEPLOYMENT
    )

    assert len(notices) == 1
    notice = notices[0]
    assert notice["service"] == "publish-on-web"
    assert notice["type"] == "subdomain"
    assert notice["status"] == "denied"
    assert notice["subject"] == "test2.robbertuittenbroek.nl"
    assert "afgewezen" in notice["text"]
    assert "standaard clusteradres" in notice["text"]
    assert notice["by"] == "admin@sandbox.rijksapp.dev"
    assert notice["message"] == "niet van dit project"


def test_requested_domain_reports_it_is_waiting():
    notices = collect_deployment_approval_notices(_project("requested", "requested"), DEPLOYMENT)

    types = {n["type"]: n for n in notices}
    assert set(types) == {"domain", "subdomain"}
    assert "wacht op goedkeuring" in types["domain"]["text"]


def test_a_deployment_without_its_own_domain_reports_nothing():
    project = _project("denied", "denied")
    assert collect_deployment_approval_notices(project, {"name": "acc"}) == []
