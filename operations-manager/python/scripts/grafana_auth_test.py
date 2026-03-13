#!/usr/bin/env python3
"""
Grafana OAuth Authentication Test Script

This script tests authentication to Grafana via SSO/OAuth and performs a simple Prometheus query.
"""

import getpass
import re
from urllib.parse import parse_qs, urlparse

import requests

GRAFANA_URL = "https://grafana.rig.prd1.gn2.quattro.rijksapps.nl"
OAUTH_LOGIN_PATH = "/login/generic_oauth"


def get_grafana_login_settings() -> dict | None:
    """Fetch Grafana's login settings to understand OAuth configuration."""
    print("\n=== Fetching Grafana Login Settings ===")

    # Try the settings endpoint
    settings_url = f"{GRAFANA_URL}/api/frontend/settings"
    try:
        resp = requests.get(settings_url, timeout=10)
        print(f"Settings endpoint status: {resp.status_code}")
        if resp.status_code == 200:
            settings = resp.json()
            oauth_info = settings.get("auth", {})
            print(f"OAuth providers configured: {list(oauth_info.keys())}")
            if "genericOauth" in oauth_info:
                print(f"Generic OAuth config: {oauth_info['genericOauth']}")
            return settings
    except Exception as e:
        print(f"Could not fetch settings: {e}")

    return None


def initiate_oauth_flow(session: requests.Session) -> str | None:
    """
    Initiate the OAuth flow by hitting the login endpoint.
    This will redirect to the identity provider.
    """
    print("\n=== Initiating OAuth Flow ===")

    login_url = f"{GRAFANA_URL}{OAUTH_LOGIN_PATH}"
    print(f"Requesting: {login_url}")

    # Don't follow redirects so we can see where it wants to go
    resp = session.get(login_url, allow_redirects=False, timeout=10)
    print(f"Status: {resp.status_code}")

    if resp.status_code in (301, 302, 303, 307, 308):
        redirect_url = resp.headers.get("Location")
        print(f"Redirect to: {redirect_url}")

        if redirect_url:
            # Parse the redirect URL to understand the OAuth provider
            parsed = urlparse(redirect_url)
            print(f"OAuth Provider Host: {parsed.netloc}")
            print(f"OAuth Path: {parsed.path}")

            # Parse query parameters
            query_params = parse_qs(parsed.query)
            print("OAuth Parameters:")
            for key, value in query_params.items():
                print(f"  {key}: {value}")

        return redirect_url
    else:
        print(f"Response headers: {dict(resp.headers)}")
        print(f"Response body (first 500 chars): {resp.text[:500]}")

    return None


def extract_form_action(html: str, current_url: str) -> str | None:
    """Extract form action URL from HTML and resolve it to absolute URL."""
    action_match = re.search(r'action="([^"]+)"', html)
    if not action_match:
        return None

    action_url = action_match.group(1)

    # Handle relative URLs
    if action_url.startswith("/"):
        parsed = urlparse(current_url)
        action_url = f"{parsed.scheme}://{parsed.netloc}{action_url}"
    elif not action_url.startswith("http"):
        # Relative to current path
        action_url = f"{current_url.rsplit('/', 1)[0]}/{action_url}"

    # HTML entities decode
    action_url = action_url.replace("&amp;", "&")

    return action_url


def handle_totp_challenge(session: requests.Session, response: requests.Response) -> requests.Response:
    """Handle TOTP/2FA challenge if present."""
    # Check if this is a TOTP form
    if "otp" in response.text.lower() or "totp" in response.text.lower() or "authenticator" in response.text.lower():
        print("\n=== TOTP Challenge Detected ===")

        action_url = extract_form_action(response.text, response.url)
        if not action_url:
            print("Could not find TOTP form action URL")
            return response

        print(f"TOTP form action: {action_url}")

        # Prompt for TOTP code
        totp_code = input("Enter TOTP code: ")

        # Submit TOTP - Keycloak uses 'otp' as the field name
        totp_data = {"otp": totp_code}

        print("Submitting TOTP code...")
        totp_resp = session.post(action_url, data=totp_data, allow_redirects=True, timeout=30)

        print(f"TOTP response status: {totp_resp.status_code}")
        print(f"Final URL after TOTP: {totp_resp.url}")

        return totp_resp

    return response


def authenticate_with_idp(session: requests.Session, idp_url: str, username: str, password: str) -> bool:
    """
    Attempt to authenticate with the identity provider.
    This handles the Keycloak login form submission including TOTP.
    """
    print("\n=== Authenticating with Identity Provider ===")

    # Follow the redirect to the IDP login page
    resp = session.get(idp_url, allow_redirects=True, timeout=10)
    print(f"IDP login page status: {resp.status_code}")

    # Check if we're at a login form
    if "form" in resp.text.lower() and ("username" in resp.text.lower() or "login" in resp.text.lower()):
        print("Found login form")

        action_url = extract_form_action(resp.text, resp.url)
        if action_url:
            print(f"Form action URL: {action_url}")

            # Submit credentials
            login_data = {
                "username": username,
                "password": password,
            }

            print("Submitting credentials...")
            login_resp = session.post(action_url, data=login_data, allow_redirects=True, timeout=30)

            print(f"Login response status: {login_resp.status_code}")
            print(f"URL after credentials: {login_resp.url}")

            # Check for TOTP challenge
            login_resp = handle_totp_challenge(session, login_resp)

            # Check if we ended up back at Grafana
            if GRAFANA_URL in login_resp.url:
                print("Successfully redirected back to Grafana!")
                return True
            elif "error" in login_resp.url.lower():
                print("Login appears to have failed (error in URL)")
                return False
            else:
                # Check for error messages in the page
                error_match = re.search(r'class="[^"]*error[^"]*"[^>]*>([^<]+)', login_resp.text, re.IGNORECASE)
                if error_match:
                    print(f"Error: {error_match.group(1)}")
                    return False

                # Check for another form (might be additional auth step)
                if "form" in login_resp.text.lower():
                    print(f"Another form detected at: {login_resp.url}")
                    print(f"Page preview: {login_resp.text[:500]}")
                    return False

                print(f"Unexpected state. URL: {login_resp.url}")
                return False
        else:
            print("Could not find form action URL")
            print(f"Page preview: {resp.text[:1000]}")
    else:
        print("No login form found or unexpected page")
        print(f"Current URL: {resp.url}")
        print(f"Page preview: {resp.text[:500]}")

    return False


def test_grafana_api(session: requests.Session):
    """Test if we have a valid Grafana session by calling the API."""
    print("\n=== Testing Grafana API Access ===")

    # Test user endpoint
    user_url = f"{GRAFANA_URL}/api/user"
    resp = session.get(user_url, timeout=10)
    print(f"User API status: {resp.status_code}")

    if resp.status_code == 200:
        user_info = resp.json()
        print(f"Logged in as: {user_info.get('login', 'unknown')}")
        print(f"Email: {user_info.get('email', 'unknown')}")
        print(f"Org Role: {user_info.get('orgRole', 'unknown')}")
        return True
    else:
        print(f"Not authenticated: {resp.text[:200]}")
        return False


def query_prometheus(session: requests.Session, query: str = "up"):
    """Execute a Prometheus query through Grafana's datasource proxy."""
    print(f"\n=== Querying Prometheus: {query} ===")

    # First, find the Prometheus/Mimir datasource
    datasources_url = f"{GRAFANA_URL}/api/datasources"
    resp = session.get(datasources_url, timeout=10)

    if resp.status_code != 200:
        print(f"Could not fetch datasources: {resp.status_code}")
        return None

    datasources = resp.json()
    print(f"Found {len(datasources)} datasources:")

    prometheus_ds = None
    for ds in datasources:
        ds_type = ds.get("type", "")
        ds_name = ds.get("name", "")
        print(f"  - {ds_name} (type: {ds_type}, uid: {ds.get('uid', 'N/A')})")

        if ds_type in ("prometheus", "mimir"):
            prometheus_ds = ds

    if not prometheus_ds:
        print("No Prometheus/Mimir datasource found")
        return None

    # Query via the datasource proxy
    ds_uid = prometheus_ds.get("uid")
    query_url = f"{GRAFANA_URL}/api/ds/query"

    query_payload = {
        "queries": [
            {
                "refId": "A",
                "datasource": {"type": prometheus_ds["type"], "uid": ds_uid},
                "expr": query,
                "instant": True,
            }
        ],
        "from": "now-5m",
        "to": "now",
    }

    resp = session.post(query_url, json=query_payload, timeout=30)
    print(f"Query status: {resp.status_code}")

    if resp.status_code == 200:
        result = resp.json()
        print(f"Query result: {result}")
        return result
    else:
        print(f"Query failed: {resp.text[:500]}")
        return None


def main():
    print("=" * 60)
    print("Grafana OAuth Authentication Test")
    print("=" * 60)
    print(f"Target: {GRAFANA_URL}")

    # Get credentials
    print("\nEnter your credentials:")
    username = input("Username: ")
    password = getpass.getpass("Password: ")

    # Create a session to maintain cookies
    session = requests.Session()

    # Step 1: Fetch Grafana settings
    get_grafana_login_settings()

    # Step 2: Initiate OAuth flow
    idp_url = initiate_oauth_flow(session)

    if not idp_url:
        print("\nFailed to initiate OAuth flow")
        return

    # Step 3: Authenticate with IDP
    if authenticate_with_idp(session, idp_url, username, password):
        print("\n Authentication successful!")

        # Step 4: Test Grafana API
        if test_grafana_api(session):
            # Step 5: Query Prometheus
            query_prometheus(session, "up")
    else:
        print("\nAuthentication failed")

    print("\n" + "=" * 60)
    print("Session cookies:")
    for cookie in session.cookies:
        value: str = cookie.value or ""
        if len(value) > 20:
            print(f"  {cookie.name}: {value[:20]}...")
        else:
            print(f"  {cookie.name}: {value}")


if __name__ == "__main__":
    main()
