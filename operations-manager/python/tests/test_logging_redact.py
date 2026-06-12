"""redact_sensitive_headers must keep credentials out of logs.

Regression guard for the ArgoCD connector leaking a live admin Bearer JWT via
`logger.debug(f"Request headers: {headers}")`.
"""

from opi.utils.logging_redact import redact_sensitive_headers


def test_masks_bearer_token_value() -> None:
    token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.secret.signature"
    out = redact_sensitive_headers({"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    assert token not in str(out)
    assert out["Authorization"] == "***REDACTED***"
    # Non-sensitive headers pass through so the log still shows request shape.
    assert out["Content-Type"] == "application/json"


def test_case_insensitive_and_multiple_sensitive_headers() -> None:
    out = redact_sensitive_headers(
        {
            "authorization": "Basic abc123",
            "X-API-Key": "supersecret",
            "Cookie": "session=deadbeef",
            "Accept": "application/json",
        }
    )
    assert out["authorization"] == "***REDACTED***"
    assert out["X-API-Key"] == "***REDACTED***"
    assert out["Cookie"] == "***REDACTED***"
    assert out["Accept"] == "application/json"
    # No secret value survives anywhere in the output.
    for leaked in ("abc123", "supersecret", "deadbeef"):
        assert leaked not in str(out)


def test_does_not_mutate_input() -> None:
    headers = {"Authorization": "Bearer x"}
    redact_sensitive_headers(headers)
    assert headers == {"Authorization": "Bearer x"}  # original untouched
