"""
Template system configuration for Operations Manager.

This module sets up Jinja2 templates with ROOS components for the operations-manager UI.
"""

from pathlib import Path

from fastapi.templating import Jinja2Templates
from jinja_roos_components import setup_components

# Get the opi package directory (operations-manager/python/opi)
OPI_DIR = Path(__file__).parent.parent
TEMPLATES_DIR = OPI_DIR / "templates"

# Create templates instance
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Setup ROOS components immediately on the global templates instance
# Enable strict validation in development/debug mode

# strict_mode = os.getenv("DEBUG", "false").lower() == "true" or os.getenv("ENVIRONMENT", "development") == "development"
strict_mode = True  # always!
setup_components(
    templates.env,
    htmx=True,
    static_url_prefix="/static/roos/",
    user_css_files=["/static/operations.css"],
    strict_validation=strict_mode,
)

# Add global variables that components might need
templates.env.globals["roos_assets_base_url"] = "/static/roos/dist/"


def setup_templates() -> Jinja2Templates:
    """
    Get the configured templates instance.

    Note: Setup is already done during module initialization.

    Returns:
        Configured Jinja2Templates instance with ROOS components
    """
    return templates


def get_templates() -> Jinja2Templates:
    """
    Get configured templates instance.

    Returns:
        Jinja2Templates instance with ROOS components
    """
    return templates
