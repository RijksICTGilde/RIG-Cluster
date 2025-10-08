#!/usr/bin/env python3
"""
Keycloak SSO test web server.

This creates a simple web server that demonstrates the full OIDC authorization code flow
with Keycloak, allowing users to login via SSO and displaying all user data returned.

Usage:
    python tests/keycloak_sso_test.py

Then visit: http://localhost:8000
"""

import asyncio
import base64
import hashlib
import json
import logging
import secrets
import urllib.parse
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

# Set up logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Test credentials
OIDC_CLIENT_ID = "wies"
OIDC_CLIENT_SECRET = "WKB6KHSWw7BegeoY2K9cYbuFPF9QnG77"
OIDC_DISCOVERY_URL = "https://keycloak.apps.digilab.network/realms/algoritmes/.well-known/openid-configuration"

# Server configuration
SERVER_HOST = "localhost"
SERVER_PORT = 8000
REDIRECT_URI = f"http://{SERVER_HOST}:{SERVER_PORT}/callback"

# In-memory session storage (for demo purposes)
sessions: dict[str, dict[str, Any]] = {}

app = FastAPI(title="Keycloak SSO Test", description="Test Keycloak SSO integration")


async def get_oidc_config() -> dict[str, Any]:
    """Fetch OIDC configuration from discovery endpoint."""
    async with httpx.AsyncClient() as client:
        response = await client.get(OIDC_DISCOVERY_URL)
        response.raise_for_status()
        return response.json()


def generate_pkce_challenge() -> tuple[str, str]:
    """Generate PKCE code verifier and challenge for secure OIDC flow."""
    # Generate code verifier (43-128 characters)
    code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("utf-8").rstrip("=")

    # Create code challenge (SHA256 hash of verifier, base64url encoded)
    code_challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode("utf-8")).digest()).decode("utf-8").rstrip("=")
    )

    return code_verifier, code_challenge


@app.get("/", response_class=HTMLResponse)
async def home():
    """Home page with login button."""
    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Keycloak SSO Test</title>
        <style>
            body { font-family: Arial, sans-serif; max-width: 800px; margin: 50px auto; padding: 20px; }
            .container { text-align: center; }
            .btn { background: #007bff; color: white; padding: 12px 24px; text-decoration: none; border-radius: 5px; display: inline-block; margin: 10px; }
            .btn:hover { background: #0056b3; }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>🔐 Keycloak SSO Test</h1>
            <p>Test the complete OIDC authorization code flow with Keycloak</p>
            <p><strong>Client ID:</strong> wies</p>
            <p><strong>Discovery URL:</strong> <a href="https://keycloak.apps.digilab.network/realms/algoritmes/.well-known/openid-configuration" target="_blank">View Config</a></p>
            <br>
            <a href="/login" class="btn">🚀 Login with Keycloak SSO</a>
            <br><br>
            <p><em>After login, you'll see all user data returned by Keycloak</em></p>
        </div>
    </body>
    </html>
    """
    return html


@app.get("/login")
async def login():
    """Initiate OIDC login flow."""
    logger.info("Starting OIDC login flow...")

    try:
        # Get OIDC configuration
        oidc_config = await get_oidc_config()
        auth_endpoint = oidc_config["authorization_endpoint"]

        # Generate session state and PKCE
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        code_verifier, code_challenge = generate_pkce_challenge()

        # Store session data
        sessions[state] = {
            "nonce": nonce,
            "code_verifier": code_verifier,
            "started_at": asyncio.get_event_loop().time(),
        }

        # Build authorization URL
        auth_params = {
            "client_id": OIDC_CLIENT_ID,
            "response_type": "code",
            "scope": "openid profile email",
            "redirect_uri": REDIRECT_URI,
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }

        auth_url = f"{auth_endpoint}?{urllib.parse.urlencode(auth_params)}"

        logger.info(f"Redirecting to Keycloak: {auth_url}")
        return RedirectResponse(url=auth_url)

    except Exception as e:
        logger.error(f"Failed to start login flow: {e}")
        raise HTTPException(status_code=500, detail=f"Login failed: {e!s}")


@app.get("/callback")
async def callback(request: Request):
    """Handle OIDC callback and exchange code for tokens."""
    logger.info("Handling OIDC callback...")

    try:
        # Get callback parameters
        code = request.query_params.get("code")
        state = request.query_params.get("state")
        error = request.query_params.get("error")

        if error:
            logger.error(f"OIDC error: {error}")
            raise HTTPException(status_code=400, detail=f"OIDC error: {error}")

        if not code or not state:
            logger.error("Missing code or state parameter")
            raise HTTPException(status_code=400, detail="Missing code or state parameter")

        # Validate session
        session_data = sessions.get(state)
        if not session_data:
            logger.error(f"Invalid or expired state: {state}")
            logger.error(f"Available sessions: {list(sessions.keys())}")
            raise HTTPException(status_code=400, detail="Invalid or expired session")

        logger.info(f"Found session for state: {state}")
        logger.info(f"Session data keys: {list(session_data.keys())}")

        # Get OIDC configuration
        oidc_config = await get_oidc_config()
        token_endpoint = oidc_config["token_endpoint"]
        userinfo_endpoint = oidc_config["userinfo_endpoint"]

        # Exchange authorization code for tokens
        token_data = {
            "grant_type": "authorization_code",
            "client_id": str(OIDC_CLIENT_ID),
            "client_secret": str(OIDC_CLIENT_SECRET),
            "code": str(code),
            "redirect_uri": str(REDIRECT_URI),
            "code_verifier": str(session_data["code_verifier"]),
        }

        logger.info("Starting token exchange...")
        logger.info(f"Token endpoint: {token_endpoint}")
        logger.info(f"Token data keys: {list(token_data.keys())}")

        async with httpx.AsyncClient() as client:
            # Get tokens
            logger.info("Making token request...")
            token_response = await client.post(
                token_endpoint, data=token_data, headers={"Content-Type": "application/x-www-form-urlencoded"}
            )
            token_response.raise_for_status()
            tokens = token_response.json()

            logger.info("Successfully exchanged code for tokens")
            logger.info(f"Token response keys: {list(tokens.keys())}")

            # Get user info
            logger.info("Getting user info...")
            userinfo_response = await client.get(
                userinfo_endpoint, headers={"Authorization": f"Bearer {tokens['access_token']}"}
            )
            userinfo_response.raise_for_status()
            user_info = userinfo_response.json()

            logger.info(f"Retrieved user info for: {user_info.get('preferred_username', 'unknown')}")
            logger.info(f"User info keys: {list(user_info.keys())}")

        # Clean up session
        del sessions[state]

        # Display results
        return display_user_data(tokens, user_info)

    except httpx.HTTPError as e:
        logger.error(f"HTTP error during token exchange: {e}")
        if hasattr(e, "response") and e.response:
            logger.error(f"Response: {e.response.text}")
        raise HTTPException(status_code=500, detail=f"Token exchange failed: {e!s}")
    except Exception as e:
        logger.error(f"Unexpected error in callback: {e}")
        raise HTTPException(status_code=500, detail=f"Callback failed: {e!s}")


def display_user_data(tokens: dict[str, Any], user_info: dict[str, Any]) -> HTMLResponse:
    """Display user data and token information."""

    # Decode JWT payload (for display purposes only - in production, verify signature!)
    try:
        access_token_parts = tokens.get("access_token", "").split(".")
        if len(access_token_parts) == 3:
            # Add padding if needed
            payload_b64 = access_token_parts[1]
            payload_b64 += "=" * (4 - len(payload_b64) % 4)
            payload = json.loads(base64.urlsafe_b64decode(payload_b64).decode())
        else:
            payload = {"error": "Could not decode JWT"}
    except Exception as e:
        payload = {"error": f"JWT decode error: {e!s}"}

    # Pre-compute filtered token data (without access_token for display)
    filtered_tokens = {k: v for k, v in tokens.items() if k != "access_token"}
    access_token_preview = str(tokens.get("access_token", "N/A"))[:50]

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Keycloak SSO Success</title>
        <style>
            body {{ font-family: Arial, sans-serif; max-width: 1000px; margin: 20px auto; padding: 20px; }}
            .success {{ background: #d4edda; border: 1px solid #c3e6cb; padding: 15px; border-radius: 5px; margin-bottom: 20px; }}
            .data-section {{ background: #f8f9fa; border: 1px solid #e9ecef; padding: 15px; margin: 15px 0; border-radius: 5px; }}
            .data-section h3 {{ margin-top: 0; color: #495057; }}
            pre {{ background: #ffffff; border: 1px solid #dee2e6; padding: 10px; border-radius: 3px; overflow-x: auto; }}
            .btn {{ background: #6c757d; color: white; padding: 10px 20px; text-decoration: none; border-radius: 5px; display: inline-block; margin: 10px 5px 0 0; }}
            .btn:hover {{ background: #545b62; }}
            .highlight {{ background: #fff3cd; padding: 2px 4px; border-radius: 2px; }}
        </style>
    </head>
    <body>
        <div class="success">
            <h2>🎉 SSO Login Successful!</h2>
            <p>Successfully authenticated with Keycloak and retrieved user data.</p>
        </div>
        
        <div class="data-section">
            <h3>👤 User Information (from /userinfo endpoint)</h3>
            <pre>{json.dumps(user_info, indent=2)}</pre>
        </div>
        
        <div class="data-section">
            <h3>🔑 Token Information</h3>
            <p><strong>Token Type:</strong> <span class="highlight">{tokens.get('token_type', 'N/A')}</span></p>
            <p><strong>Expires In:</strong> <span class="highlight">{tokens.get('expires_in', 'N/A')} seconds</span></p>
            <p><strong>Refresh Token:</strong> <span class="highlight">{'Present' if tokens.get('refresh_token') else 'Not provided'}</span></p>
            <p><strong>Scope:</strong> <span class="highlight">{tokens.get('scope', 'N/A')}</span></p>
        </div>
        
        <div class="data-section">
            <h3>📋 Access Token Claims (decoded JWT payload)</h3>
            <p><em>Note: In production, always verify JWT signature before trusting claims!</em></p>
            <pre>{json.dumps(payload, indent=2)}</pre>
        </div>
        
        <div class="data-section">
            <h3>🔧 Raw Token Response</h3>
            <pre>{json.dumps(filtered_tokens, indent=2)}</pre>
            <p><strong>Access Token:</strong> {access_token_preview}... (truncated for display)</p>
        </div>
        
        <a href="/" class="btn">🏠 Back to Home</a>
        <a href="/login" class="btn">🔄 Login Again</a>
    </body>
    </html>
    """

    return HTMLResponse(content=html)


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "ok", "message": "Keycloak SSO test server is running"}


async def main():
    """Start the test server."""
    logger.info("🚀 Starting Keycloak SSO test server...")
    logger.info(f"Server will be available at: http://{SERVER_HOST}:{SERVER_PORT}")
    logger.info(f"Redirect URI configured as: {REDIRECT_URI}")
    logger.info(f"Using Keycloak client ID: {OIDC_CLIENT_ID}")

    # Test connectivity before starting
    try:
        logger.info("Testing Keycloak connectivity...")
        async with httpx.AsyncClient() as client:
            response = await client.get(OIDC_DISCOVERY_URL)
            response.raise_for_status()
            config = response.json()
            logger.info(f"✅ Keycloak accessible - Issuer: {config.get('issuer')}")
    except Exception as e:
        logger.error(f"❌ Cannot connect to Keycloak: {e}")
        logger.error("Please check your network connection and Keycloak URL")
        return

    # Start server
    config = uvicorn.Config(app, host=SERVER_HOST, port=SERVER_PORT, log_level="info", access_log=True)
    server = uvicorn.Server(config)

    logger.info("✅ Server starting...")
    logger.info("Visit http://localhost:8000 to test SSO login")
    logger.info("Press Ctrl+C to stop the server")

    await server.serve()


if __name__ == "__main__":
    asyncio.run(main())
