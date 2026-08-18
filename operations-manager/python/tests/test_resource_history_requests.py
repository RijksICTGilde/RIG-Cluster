"""Een historie-item van de resource-tuner past in het schema.

Gemeten op productie, nacht van 17 op 18 augustus 2026: om 01:00 draait de resource-tuner,
en daarna stonden er 25 waarschuwingen in het log, een per project:

    Persisting project 'algor-1ha' despite a validation failure (enforce_validation=False);
    Veld 'deployments/0/components/1/resources/history/0':
    Additional properties are not allowed ('requests' was unexpected)

De tuner schrijft `requests` bewust mee (resource_tuning_service.py: "a change that only
moves the request otherwise reads as a no-op"), maar `resource-history-entry` kende alleen
`limits` en stond met `additionalProperties: false` geen extra velden toe.

Dat is dezelfde soort fout als het registry-gat van juni: de code schrijft een veld dat het
schema afkeurt. Toen blokkeerde dat stil alle deploys van een project; nu wordt het met
enforce_validation=False alsnog weggeschreven, dus het viel alleen op als waarschuwing. Dat
maakt het minder erg maar niet minder fout: elk van die 25 projectbestanden is nu formeel
ongeldig.
"""

import pytest
from opi.core.project_schema import ProjectSchemaError, validate_project_schema


def _project(entry: dict) -> dict:
    return {
        "name": "algor-1ha",
        "components": [{"name": "web", "image": "nginx:1.25"}],
        "deployments": [
            {
                "name": "productie",
                "cluster": "odcn-production",
                "namespace": "algor-1ha",
                "components": [{"reference": "web", "resources": {"history": [entry]}}],
            }
        ],
    }


def test_de_tuner_schrijft_limits_en_requests_en_dat_mag() -> None:
    """Precies het item dat de tuner wegschrijft, met beide blokken."""
    validate_project_schema(
        _project(
            {
                "timestamp": "2026-08-18T01:00:01.383+00:00",
                "source": "auto-tune",
                "limits": {"memory": "512Mi", "cpu": "500m"},
                "requests": {"memory": "256Mi", "cpu": "250m"},
                "reason": "p95 memory 180Mi over 7 days",
            }
        )
    )


def test_alleen_limits_blijft_geldig() -> None:
    """De oude vorm moet blijven werken; bestaande projectbestanden dragen hem."""
    validate_project_schema(
        _project({"timestamp": "2026-06-01T01:00:00+00:00", "source": "oom-watcher", "limits": {"memory": "1Gi"}})
    )


def test_een_onbekend_veld_wordt_nog_steeds_geweigerd() -> None:
    """De poort blijft dicht; hier is alleen `requests` doorgelaten, niet alles."""
    with pytest.raises(ProjectSchemaError):
        validate_project_schema(
            _project({"timestamp": "2026-08-18T01:00:01+00:00", "source": "auto-tune", "verzonnen": {"a": "b"}})
        )


def test_requests_kent_alleen_geheugen_en_cpu() -> None:
    """Gespiegeld aan limits, zodat de twee niet uit elkaar kunnen lopen."""
    with pytest.raises(ProjectSchemaError):
        validate_project_schema(
            _project(
                {
                    "timestamp": "2026-08-18T01:00:01+00:00",
                    "source": "auto-tune",
                    "requests": {"schijf": "10Gi"},
                }
            )
        )
